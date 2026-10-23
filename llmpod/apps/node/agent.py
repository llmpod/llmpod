"""NodeAgent：声明式任务编排器（Node 侧自治执行的核心）。

职责：
- ``handle_ensure``：收 ``ensure_task`` → 落 local_tasks（幂等去重）→ 派发 Executor；
  ``secrets`` 仅内存（缓存在 ``_secrets``，终态即清，**永不落盘**）。
- ``handle_cancel``：置 ``cancel_event`` → Executor 检测后清 ``.part`` + 报 CANCELLED。
- ``run()``：启动恢复（``_recover``）+ 重连 sync（检测 channel False→True → ``sync_node_state``）。

启动恢复（``_recover``）：重启后 executor 进程已死、secrets 丢失。对在途（非 PENDING）任务调
``executor.reconcile``（无 secrets 探测）——命中终态 → 上报；未命中 → FAILED（"重启中断"），
由 manager attempt 机制重新下发。PENDING（未启动）任务不处理——留给 manager ``redrive_pending``
（sync_node_state）重新 ensure_task。

与 ManagerLink 的 client 绑定延迟到 ``attach_client``（client 需先于其构造，但 TaskService
注册又需 agent → 用 late-bind 打破环）。
"""

from __future__ import annotations

import asyncio
import logging

from llmpod.apps.node.agent_store import TaskStore
from llmpod.apps.node.executors import default_registry
from llmpod.apps.node.executors.base import Executor, parse_payload
from llmpod.apps.node.models import LocalTask
from llmpod.apps.node.manager_link import ManagerLink
from llmpod.tunnel.schemas import (
    Ack,
    InstanceStatusPayload,
    StartInstancePayload,
    StopInstancePayload,
    TaskEventPayload,
    TaskPayload,
)
from llmpod.config.constants import TaskStatus, TaskType

log = logging.getLogger("llmpod.apps.node.agent")

_TERMINAL_ERROR = (TaskStatus.FAILED, TaskStatus.CANCELLED)


class NodeAgent:
    """声明式任务编排器（持有 store / executors / manager_link）。"""

    def __init__(
        self,
        store: TaskStore,
        executors: dict[str, Executor] | None = None,
        *,
        manager_link: ManagerLink | None = None,
        sync_interval: float = 5.0,
    ):
        self._store = store
        self._executors = executors or default_registry()
        self._manager_link: ManagerLink | None = manager_link
        self._sync_interval = sync_interval
        # 运行态（均以 task_id 索引；secrets 仅内存，终态即清）
        self._inflight: set[str] = set()  # 去重集合（sync 增删，先于任何 await）
        self._running: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._secrets: dict[str, dict | None] = {}
        self._was_online = False
        self._maintenance = False  # 升级维护窗口：拒新任务 + suspend 时中断在跑

    def attach_client(self, client) -> None:
        """延迟绑定 TunnelClient（构造 ManagerLink；打破 client↔registry↔agent 环）。"""
        self._manager_link = ManagerLink(client)

    # ---- 升级维护模式（upgrader 替换流程的挂起/恢复钩子）----

    async def suspend_for_upgrade(self) -> None:
        """进入维护：取消全部在跑任务（标记中断，不等不候——等就绪阶段 sqlite 已
        关闭，任务必须先停否则状态写失败；恢复交给新容器启动恢复 + manager 对账
        重派），随后关闭 sqlite 连接（新容器独占数据卷）。"""
        self._maintenance = True
        active = [t for t in self._running.values() if not t.done()]
        if active:
            log.info("维护模式：中断 %d 个在跑任务（INTERRUPTED，等新容器恢复）", len(active))
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
        await self._store.close()
        log.info("维护模式：在跑任务已中断，sqlite 已关闭")

    async def resume_after_upgrade(self) -> None:
        """退出维护：重开 sqlite + 恢复接收任务（失败回滚路径用）。"""
        await self._store.init()
        self._maintenance = False
        log.info("维护模式退出：sqlite 已重开")

    @property
    def maintenance(self) -> bool:
        return self._maintenance

    # ---- TaskService 委托入口 ----

    async def handle_ensure(self, task: TaskPayload, secrets: dict | None = None) -> Ack:
        """声明式下发：幂等去重 + 落库 + 派发 executor。"""
        if task.id in self._inflight:
            log.debug("handle_ensure: task %s 已在执行，幂等跳过", task.id[:8])
            return Ack(ok=True)
        self._inflight.add(task.id)  # sync 占位，先于任何 await（防并发双派发）
        local = LocalTask.from_payload(task)
        self._cancel_events[task.id] = asyncio.Event()
        self._secrets[task.id] = secrets
        try:
            await self._store.upsert(local)  # 幂等：active 行不覆盖执行期进度
        except Exception:
            self._inflight.discard(task.id)
            self._cancel_events.pop(task.id, None)
            self._secrets.pop(task.id, None)
            raise
        self._running[task.id] = asyncio.create_task(self._run_task(local))
        log.info("handle_ensure: 派发 task %s type=%s", task.id[:8], task.type)
        return Ack(ok=True)

    async def handle_cancel(self, task_id: str) -> Ack:
        """取消：置 cancel_event（executor 检测后清 .part + 报 CANCELLED）。best-effort。"""
        ev = self._cancel_events.get(task_id)
        if ev is not None:
            ev.set()
            log.info("handle_cancel: task %s cancel 已置", task_id[:8])
        else:
            log.debug("handle_cancel: task %s 未在执行（无 cancel_event）", task_id[:8])
        return Ack(ok=True)

    # ---- 执行 + 上报 ----

    async def _run_task(self, local: LocalTask) -> None:
        cancel_event = self._cancel_events.get(local.task_id) or asyncio.Event()
        secrets = self._secrets.get(local.task_id)
        executor = self._executors.get(local.type)
        try:
            if executor is None:
                await self._emit(local, TaskStatus.FAILED, message=f"无可执行器: type={local.type}")
                return
            await self._run_executor(executor, local, secrets, cancel_event)
        except asyncio.TimeoutError:
            # 任务超时（_run_executor 的 wait_for 触发）：执行器协程已被取消；置 cancel + 报 FAILED。
            # 容器/.part 残留由 reconcile/stop 收敛（START_INSTANCE 的容器残留属已知限制）。
            cancel_event.set()
            await self._emit(local, TaskStatus.FAILED, message=f"任务超时（{local.timeout_sec}s）")
            return
        except asyncio.CancelledError:
            # 真正的任务取消（非 cancel_event 信号）：执行器自清中间态（on_cancel
            # 默认 no-op，ENSURE_MODEL 清 .part 等）+ 报 CANCELLED 后重抛
            if executor is not None:
                try:
                    await executor.on_cancel(local)
                except Exception:
                    log.warning("on_cancel 清理失败 task=%s", local.task_id[:8])
            await self._emit(local, TaskStatus.CANCELLED, message="任务已取消")
            raise
        except Exception as e:
            # 不用 log.exception：完整 traceback 可能携带 secret（异常 repr / 帧局部）。
            # message 经 _emit 再脱敏一次（亦覆盖 executor 自报的 FAILED message）。
            msg = f"执行器异常: {e}"
            log.warning("executor 异常 task=%s: %s", local.task_id[:8], self._scrub_secrets(local.task_id, msg))
            await self._emit(local, TaskStatus.FAILED, message=msg)
        finally:
            self._inflight.discard(local.task_id)
            self._running.pop(local.task_id, None)
            self._cancel_events.pop(local.task_id, None)
            self._secrets.pop(local.task_id, None)  # secrets 即清（仅内存）

    async def _run_executor(self, executor, local: LocalTask, secrets, cancel_event) -> None:
        """驱动执行器到终态；按 ``task.timeout_sec`` 强制超时（None/0 不限时）。

        超时由 ``asyncio.wait_for`` 触发——它取消执行器协程并抛 ``TimeoutError``，由 ``_run_task``
        的对应 except 捕获报 FAILED（避免拉取/下载 stall 时任务永久 RUNNING、工人槽不回收）。
        """
        timeout = local.timeout_sec or 0
        coro = executor.execute(local, secrets, self._make_report(local), cancel_event)
        if timeout > 0:
            await asyncio.wait_for(coro, timeout=timeout)
        else:
            await coro

    def _make_report(self, local: LocalTask):
        async def report(
            status,
            progress=0,
            *,
            stage=None,
            message=None,
            instance_status=None,
            container_id=None,
            endpoint=None,
            extra=None,
        ):
            await self._emit(
                local,
                status,
                progress,
                stage=stage,
                message=message,
                instance_status=instance_status,
                container_id=container_id,
                endpoint=endpoint,
                extra=extra,
            )

        return report

    def _scrub_secrets(self, task_id: str, text: str | None) -> str | None:
        """把已知 secret 明文从文本中抹除（防止 token 经异常消息落盘/上报/入日志）。

        仅抹除本 task 已缓存的 secret 值（``_secrets``）——executor 抛出的异常文本可能
        携带 token（如 HF 下载失败时库回显请求上下文），不得进入 local_tasks.last_error
        （持久化）/ TaskEventPayload.message（上报）。对 HF/本地源下载等会回显请求上下文的执行器生效。
        """
        if not text:
            return text
        out = text
        for v in (self._secrets.get(task_id) or {}).values():
            if isinstance(v, str) and v:
                out = out.replace(v, "***")
        return out

    async def _emit(
        self,
        local: LocalTask,
        status: str,
        progress: int = 0,
        *,
        stage: str | None = None,
        message: str | None = None,
        instance_status: str | None = None,
        container_id: str | None = None,
        endpoint: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """落 local_tasks + 上报 manager（store 失败不阻断上报；manager 失败 best-effort）。

        结构化标记载荷（instance_status 等）随 extra 一并入 ``TaskEventPayload.extra``
        （审计权威来源），instance_status 非空时额外经 ``report_instance_status`` 独立通道
        上报驱动 instance 表（两者丢失均由 reconcile 收敛）。
        """
        message = self._scrub_secrets(local.task_id, message)
        last_error = message if status in _TERMINAL_ERROR else None
        try:
            await self._store.update_status(
                local.task_id,
                status,
                progress=progress,
                last_error=last_error,
            )
        except Exception as e:
            log.warning("emit: store 更新失败 task=%s: %s", local.task_id[:8], e)
        if self._manager_link is not None:
            merged = dict(extra or {})
            # 具名结构化载荷有独立通道（下方 instance_status 分支），塞进 extra 只会
            # 落审计轨迹而驱动不了 instance 表——这里拦一道，把「键名写错侧」变成显式告警
            leaked = [k for k in ("instance_status", "container_id", "endpoint") if k in merged]
            if leaked:
                log.warning("emit: extra 携带结构化载荷键 %s（应走具名参数）task=%s", leaked, local.task_id[:8])
            if instance_status is not None:
                merged["instance_status"] = instance_status
            if container_id is not None:
                merged["container_id"] = container_id
            if endpoint is not None:
                merged["endpoint"] = endpoint
            evt = TaskEventPayload(
                task_id=local.task_id,
                node_id=local.node_id,
                status=status,
                stage=stage,
                progress=progress,
                resource_type=local.resource_type,
                resource_key=local.resource_key,
                message=message,
                extra=merged or None,
            )
            await self._manager_link.report_task_event(evt)  # 内部 best-effort
            # 实例长期状态迁移：instance_status 触发独立通道上报（START/STOP 配方为
            # typed 契约，从中取实例标识；校验失败仅告警跳过，不误报空标识）。
            if instance_status:
                refs = self._instance_refs(local)
                if refs is not None:
                    deployment_id, instance_id = refs
                    inst_evt = InstanceStatusPayload(
                        deployment_id=deployment_id,
                        instance_id=instance_id,
                        node_id=local.node_id,
                        status=instance_status,
                        container_id=container_id,
                        endpoint=endpoint,
                        message=message,
                    )
                    await self._manager_link.report_instance_status(inst_evt)

    def _instance_refs(self, local: LocalTask) -> tuple[str, str] | None:
        """从任务配方取 ``(deployment_id, instance_id)``（配方不合法时跳过，parse_payload 已告警）。"""
        payload_cls = {
            TaskType.START_INSTANCE: StartInstancePayload,
            TaskType.STOP_INSTANCE: StopInstancePayload,
        }.get(local.type)
        if payload_cls is None:
            return None
        p = parse_payload(local, payload_cls)
        return (p.deployment_id, p.instance_id) if p is not None else None

    # ---- 启动恢复 + 重连 sync（run loop）----

    async def run(self) -> None:
        """后台循环：重连（channel False→True）时先恢复（emits 走在线 channel）再 sync。

        启动恢复推迟到首次 channel 就绪——否则 ``_recover`` 的 report_task_event（携带
        model_path）会因 channel 未通被丢弃；而 manager 的 ``sync_node_state`` 不回填终态/
        model_path（只有 ``report_task_event`` 会投影 ``node_model``，见 task_ingest）。recover
        在循环 ``try/except`` 内执行，单行异常不致命（见 ``_recover`` 逐行容错）。
        """
        recovered = False
        while True:
            try:
                online = self._manager_link.online if self._manager_link else False
                # 升级维护窗口 sqlite 已关闭，读写本地库的动作全部跳过（store 未 init 会抛）
                if online and not self._was_online and not self._maintenance:
                    if not recovered:
                        await self._recover()  # 首次重连：channel 已通，emits 不丢
                        recovered = True
                    await self._sync_to_manager()
                self._was_online = online
            except Exception as e:
                log.debug("agent run loop: %s", e)
            await asyncio.sleep(self._sync_interval)

    async def _recover(self) -> None:
        """重启恢复：在途（非 PENDING）任务 ``executor.reconcile``（无 secrets）→ 终态/FAILED；PENDING 留给 redrive。

        - 处理前将各 active 任务 id 占入 ``_inflight``（sync，先于任何 await），防止恢复期间
          并发 ``ensure_task`` 对同 id 双派发；处理完即释放（终态行交由后续 redrive/sync 收敛）。
        - 逐行容错：单行异常（如 local_dir 不可读的 PermissionError）不中断其余行恢复。
        """
        active = await self._store.list_active()
        claimed = [t for t in active if t.status != TaskStatus.PENDING]
        for t in claimed:  # 先占位（sync，先于任何 await）
            self._inflight.add(t.task_id)
        for t in claimed:
            try:
                executor = self._executors.get(t.type)
                if executor is None:
                    await self._emit(t, TaskStatus.FAILED, message=f"无执行器(恢复): type={t.type}")
                    continue
                result = await executor.reconcile(t)  # 无 secrets 探测（容器类允许只读 I/O）
                if result:
                    log.info("recover: task %s 已就位 → %s", t.task_id[:8], result.status)
                    await self._emit(
                        t,
                        result.status,
                        result.progress,
                        instance_status=result.instance_status,
                        container_id=result.container_id,
                        endpoint=result.endpoint,
                        extra=result.extra,
                    )
                else:
                    log.info("recover: task %s 重启中断 → FAILED（等重新下发）", t.task_id[:8])
                    await self._emit(t, TaskStatus.FAILED, message="node 重启中断，等待重新下发")
            except Exception as e:
                log.warning("recover: task %s 恢复异常: %s", t.task_id[:8], self._scrub_secrets(t.task_id, str(e)))
            finally:
                self._inflight.discard(t.task_id)

    async def _sync_to_manager(self) -> None:
        if self._manager_link is None:
            return
        tasks = [t.to_state() for t in await self._store.list_all()]
        try:
            await self._manager_link.sync_node_state(tasks)
        except Exception as e:
            log.debug("sync_to_manager: %s", e)
