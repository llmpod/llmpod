"""START_INSTANCE 执行器：启动推理容器 + Node 本地 readiness 探活。

Node 自治判定 Ready（Manager 只做 gate）：``deploy(spec)`` 起容器（同名幂等替换）→
按 readiness 配置探活 → 通过即报 READY + task SUCCESS。两种模式（payload
``readiness.mode``，缺省 http 兼容旧 payload）：

- ``http``：轮询 HTTP 端点 200（path/超时由 Manager 经 payload 下发）——有 API
  端口的实例（head/单节点 full）；
- ``stable_running``：容器存活稳定判定（连续 N 次 stable：Status==running 且
  Running 且非 Restarting 且 RestartCount==0；RestartCount>0 立即失败——restart
  loop 快失败，配合 restart_policy=no）——无 HTTP 端口的 headless worker。

探活循环每 tick 先查 cancel（再查存活——避免与 STOP_INSTANCE 的 stop 竞态时把
「被停止」误报为「崩溃」），容器退出/restart-loop/超时一律 **stop-only（不 rm）**
保留崩溃容器——日志文件随容器保留至 ``rm`` 前，前端点容器日志按钮经 RPC（
``NodeService.tail_log`` / ``ContainerService.logs``）即可取全量崩溃输出，无需持久化
引擎 stdout。

取消路径仍 stop+rm（用户主动取消，无需留痕）。崩溃/超时容器的最终清理由部署级
``cleanup_deployment_containers``（label 扫除）/重试时同名 409-replace 兜底——stopped
容器不占 GPU，仅占磁盘。``reconcile``（重启恢复）仅在「健康」时判 SUCCESS——http
模式需探活通过；stable 模式需存活稳定——避免误杀健康容器或把半加载容器误判就绪。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from llmpod.apps.node.deployers.base import Deployer
from llmpod.apps.node.exceptions import ContainerGone
from llmpod.apps.node.executors.base import Executor, ReconcileResult, ReportFn, parse_payload
from llmpod.apps.node.models import LocalTask
from llmpod.tunnel.schemas import ContainerState, StartInstancePayload
from llmpod.config.constants import TaskStatus, InstanceStatus

log = logging.getLogger("llmpod.apps.node.executors.start_instance")

_GATE_CONSECUTIVE = 3  # stable_running 模式：连续 N 次稳定判定才认就绪

ProbeFn = Callable[[str], Awaitable[bool]]


def _is_stable(state: ContainerState) -> bool:
    """容器「稳定运行」：Status==running 且 Running 且非 Restarting 且未重启过。"""
    return state.Status == "running" and state.Running is True and state.Restarting is False and not state.RestartCount


async def _sleep_cancelable(seconds: float, cancel_event: asyncio.Event) -> bool:
    """等待 ``seconds`` 秒；期间 cancel_event 命中立即返回 True。"""
    if seconds <= 0:
        return cancel_event.is_set()
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


class StartInstanceExecutor(Executor):
    """START_INSTANCE 执行器（构造注入 Deployer；``prober`` 可注入供测试）。"""

    def __init__(self, deployer: Deployer, *, prober: ProbeFn | None = None):
        self._deployer = deployer
        self._prober: ProbeFn = prober or self._default_probe

    async def execute(
        self,
        task: LocalTask,
        secrets: dict | None,
        report: ReportFn,
        cancel_event: asyncio.Event,
    ) -> None:
        payload = parse_payload(task, StartInstancePayload)
        if payload is None:
            await report(TaskStatus.FAILED, message="START_INSTANCE 配方无效")
            return
        container_name = payload.container_name
        spec = payload.spec  # 已是 DeploySpec（payload 校验时嵌套完成）
        readiness = payload.readiness
        endpoint = payload.endpoint

        if not container_name:
            await report(
                TaskStatus.FAILED,
                message="START_INSTANCE 缺少 container_name",
                instance_status=InstanceStatus.FAILED,
            )
            return

        if cancel_event.is_set():
            await report(TaskStatus.CANCELLED, message="任务已取消")
            return

        # 幂等短路：容器已在运行则不重建——重发的 START 直接进 readiness 判定，
        # 避免把正在对外服务的容器同名替换、中断推理。仅 ContainerGone（容器确实
        # 不存在）才当「未运行」；daemon 级异常（超时/网络抖动）时容器状态未知——
        # 误判未运行会 409-replace 在服容器或对运行中容器误报 FAILED 释放卡，
        # 与 stop / readiness 循环同口径保守失败待重试
        try:
            already_running = bool((await self._deployer.inspect(container_name)).Running)
        except ContainerGone:
            already_running = False
        except Exception as e:
            log.warning("pre-deploy inspect %s 失败（容器状态未知，不 deploy）: %s", container_name, e)
            await report(
                TaskStatus.FAILED,
                message=f"容器状态查询失败（无法判断是否已运行，待重试）: {e}",
                instance_status=InstanceStatus.FAILED,
            )
            return

        await report(TaskStatus.RUNNING, instance_status=InstanceStatus.STARTING)
        if not already_running:
            try:
                await self._deployer.deploy(spec)
            except Exception as e:
                await report(
                    TaskStatus.FAILED,
                    message=f"容器启动失败: {e}",
                    instance_status=InstanceStatus.FAILED,
                )
                return
        if cancel_event.is_set():
            await self._cancel_cleanup(container_name, report)
            return
        await report(
            TaskStatus.RUNNING,
            instance_status=InstanceStatus.RUNNING,
            container_id=container_name,
        )

        # readiness 探活：每 tick 先 cancel 检查点（后存活判定）→ 按模式判就绪 → 超时判定。
        # 数值默认收敛在 ReadinessConfig 字段默认值（0 是合法值——如 interval_sec=0
        # 测试瞬时轮询——不能 `or` 兜底，0 会被误吞成默认值）。
        mode = readiness.mode
        url = readiness.url or ""
        timeout = readiness.timeout_sec
        interval = readiness.interval_sec
        delay = readiness.initial_delay_sec
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        if await _sleep_cancelable(delay, cancel_event):
            await self._cancel_cleanup(container_name, report)
            return

        last_probe_error = ""
        stable_hits = 0
        while True:
            if cancel_event.is_set():
                await self._cancel_cleanup(container_name, report)
                return
            # 存活判定：inspect 异常（docker daemon 瞬时抖动）≠ 容器退出——跳过本轮，不误判崩溃，
            # 仅 inspect 成功且 Running=False 才算引擎崩溃（抓日志尾部后判失败，不等探活超时）。
            # inspect 持续失败受下方 deadline 超时约束，不会无限轮询。
            exit_code: int | None = None
            inspect_ok = True
            try:
                state = await self._deployer.inspect(container_name)
            except Exception as e:
                inspect_ok = False
                state = None
                log.warning("readiness inspect %s 查询失败（跳过本轮，不误判崩溃）: %s", container_name, e)
            if inspect_ok and not bool(state.Running):
                exit_code = state.ExitCode
                # 保留崩溃容器（只 stop 不 rm）：日志文件随容器留存，供前端点日志按钮经
                # RPC 取全量崩溃输出。清理由部署级 label 扫除 / 重试同名 409-replace 兜底。
                await self._stop_container(container_name)
                await report(
                    TaskStatus.FAILED,
                    message=(
                        f"容器 {container_name} 在 readiness 探活期间退出（引擎崩溃，"
                        f"ExitCode={exit_code}）—— 容器已保留，详见容器日志"
                    ),
                    instance_status=InstanceStatus.FAILED,
                )
                return

            # inspect 成功才推进就绪判定（失败本轮仅等下一 tick；稳定计数归零避免误判就绪）
            if inspect_ok:
                if mode == "stable_running":
                    # restart-loop 快失败：restart_policy=no 下 RestartCount>0 即引擎反复崩溃
                    rc = int(state.RestartCount or 0)
                    if rc > 0:
                        # 保留崩溃容器（只 stop 不 rm）供 RPC 取全量日志。
                        await self._stop_container(container_name)
                        await report(
                            TaskStatus.FAILED,
                            message=(f"容器 {container_name} 反复重启（RestartCount={rc}）—— 容器已保留，详见容器日志"),
                            instance_status=InstanceStatus.FAILED,
                        )
                        return
                    # 连续稳定计数（抖动归零）
                    if _is_stable(state):
                        stable_hits += 1
                    else:
                        stable_hits = 0
                    if stable_hits >= _GATE_CONSECUTIVE:
                        await report(
                            TaskStatus.SUCCESS,
                            100,
                            instance_status=InstanceStatus.READY,
                            endpoint=endpoint,
                        )
                        return
                else:
                    try:
                        if await self._prober(url):
                            await report(
                                TaskStatus.SUCCESS,
                                100,
                                instance_status=InstanceStatus.READY,
                                endpoint=endpoint,
                            )
                            return
                        last_probe_error = ""
                    except Exception as e:
                        last_probe_error = str(e)
            else:
                stable_hits = 0

            if loop.time() >= deadline:
                # 超时保留容器（只 stop 不 rm）：供 RPC 取日志排查「卡在哪」（引擎半加载等）。
                await self._stop_container(container_name)
                if mode == "stable_running":
                    msg = f"readiness 超时（{timeout:.0f}s）：容器未达到连续 {_GATE_CONSECUTIVE} 次稳定运行"
                else:
                    msg = f"readiness 超时（{timeout:.0f}s）: {url}"
                    if last_probe_error:
                        msg += f"（最后一次探测: {last_probe_error}）"
                # instance_status 必须走命名参数通道（驱动 instance 行落 FAILED）：
                # 塞 extra 无任何消费方，实例行会停在 RUNNING，续驱/自愈只认死态行，
                # 该分片从此无人重下发（本文件其余失败分支均走命名参数，此处对齐）
                await report(TaskStatus.FAILED, message=msg, instance_status=InstanceStatus.FAILED)
                return
            if await _sleep_cancelable(interval, cancel_event):
                await self._cancel_cleanup(container_name, report)
                return

    async def reconcile(self, task: LocalTask) -> ReconcileResult | None:
        """重启探测：健康容器 → SUCCESS(+READY)；否则 None（等重新下发）。

        http 模式需「Running 且探活通过」；stable 模式需「存活稳定（RestartCount==0）」。
        只在「健康」时判达成——半加载容器回 None → Manager 重发，重发的 deploy 同名
        替换，等价于接续等待就绪，避免误判 RUNNING 为部署完成或重启误杀健康容器。
        """
        payload = parse_payload(task, StartInstancePayload)
        if payload is None:
            return None
        name = payload.container_name
        if not name:
            return None
        try:
            state = await self._deployer.inspect(name)
        except Exception:
            return None  # 容器不存在/查询失败 → 未达成
        if not state.Running:
            return None
        if payload.readiness.mode == "stable_running":
            if state.Restarting or state.RestartCount:
                return None  # 重启中/重启过 → 不算健康，等重新下发（重发会快失败或接续）
            return ReconcileResult(
                TaskStatus.SUCCESS,
                instance_status=InstanceStatus.READY,
                endpoint=payload.endpoint,
            )
        url = payload.readiness.url or ""
        if url:
            try:
                if not await self._prober(url):
                    return None
            except Exception:
                return None
        return ReconcileResult(
            TaskStatus.SUCCESS,
            instance_status=InstanceStatus.READY,
            endpoint=payload.endpoint,
        )

    # ---- helpers ----

    async def _cancel_cleanup(self, name: str, report: ReportFn) -> None:
        """用户取消收口（四处触发点共用）：stop+rm 容器 + 报 CANCELLED/STOPPED。

        instance_status 走命名参数通道（驱动实例行落 STOPPED）：塞 extra 无消费方，
        实例行停在 RUNNING/STARTING 会被自愈当异常分片重下发，把刚取消的容器又
        起回来。取消路径 stop+rm（用户主动取消，无需留痕），区别于崩溃/超时的
        stop-only 保留容器供取日志。
        """
        await self._cleanup_container(name)
        await report(TaskStatus.CANCELLED, message="任务已取消", instance_status=InstanceStatus.STOPPED)

    async def _cleanup_container(self, name: str) -> None:
        """best-effort stop+rm（NotFound 容忍；失败仅日志，不阻断终态上报）。"""
        try:
            await self._deployer.stop(name)
        except Exception as e:
            log.debug("cleanup stop %s: %s", name, e)
        try:
            await self._deployer.remove(name, force=True)
        except Exception as e:
            log.debug("cleanup remove %s: %s", name, e)

    async def _stop_container(self, name: str) -> None:
        """best-effort 只 stop 不 rm——保留崩溃/超时容器，供前端点日志按钮经 RPC 取全量日志。

        失败（超时/daemon 无响应）仅 ``log.warning`` 不阻断：容器多半已退出，stop 失败
        不影响日志保留。清理由部署级 ``cleanup_deployment_containers`` / 重试同名 409-replace 兜底。
        """
        try:
            await self._deployer.stop(name)
        except Exception as e:
            log.warning("stop-only %s 失败（容器可能已退出，日志仍保留）: %s", name, e)

    async def _default_probe(self, url: str) -> bool:
        """默认 readiness 探测：HTTP GET 200 即就绪（谓词由 Manager 下发 path 决定）。"""
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url)
            return r.status_code == 200
