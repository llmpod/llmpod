"""节点级操作服务实现：信息查询 + shell + 流式。

info 复用 collector（保证 RPC 查询与 $heartbeat 口径一致）；execute_shell 用
asyncio.subprocess；docker 操作（docker_pull/tail_log）一律走 SDK 经挂载的
docker.sock——生产拓扑 node 跑在容器内、没有 docker CLI，不允许起 docker 命令子进程。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from docker.errors import NotFound

from llmpod.apps.node.deployers.docker import (
    STREAM_DONE,
    get_docker_client,
    image_present_sync,
    iter_pull_chunks,
    next_stream_chunk,
)
from llmpod.apps.node.exceptions import TaskExecutionError
from llmpod.config.settings import Settings
from llmpod.tunnel.contract import NodeService
from llmpod.tunnel.schemas import NodeReport, ShellResult

log = logging.getLogger("llmpod.apps.node.service.node")

# ---- tail_log 参数（docker SDK follow 长连接，无手工游标）----

_TAIL_POLL_INTERVAL_S = 1.0  # follow 流结束后重开轮询间隔
_TAIL_MISSING_POLL_INTERVAL_S = 3.0  # 容器不存在（部署准备/已删/重建窗口）时降频
_TAIL_DIAG_EVERY = 5  # 非 running 态每 N 轮吐一条诊断行（防前端只见静默）
# 复活窗口：非 running 持续超此值（自最后一次 running 起）→ 结束流。新部署容器
# 创建、崩溃 backoff、rm+create 重试间隔可达分钟级，取 10 分钟兜底；更长（如大
# 模型下载期未建容器）由前端在收到 end 后按部署未终态重开。
_TAIL_REVIVE_WINDOW_S = 600.0

# 容器未创建时的诊断文案（部署准备/重建窗口；降频轮询期间节流输出）
_TAIL_WAIT_MESSAGE = "尚未创建（部署准备中）"


def _format_pull_chunk(chunk) -> str:
    """SDK pull chunk（decode=True 的 dict）→ 可读文本行（保持逐行 wire 契约）。"""
    if not isinstance(chunk, dict):
        return str(chunk)
    parts = [chunk.get("status") or ""]
    if chunk.get("id"):
        parts.append(str(chunk["id"]))
    if chunk.get("progress"):
        parts.append(str(chunk["progress"]).replace("\r", ""))
    return " ".join(p for p in parts if p)


class NodeServiceImpl(NodeService):
    """节点级操作 RPC 实现。"""

    def __init__(self, settings: Settings, node_id_provider=None):
        self.settings = settings
        # node_id 首启为空、$register_ack 后才由 Manager 分配（落在 TunnelClient 上）；
        # 构造期快照会把空值固化到进程重启——经回调读 live 值，无回调时回落 settings
        self._node_id_provider = node_id_provider
        self._started = time.time()

    @property
    def node_id(self) -> str:
        if self._node_id_provider is not None:
            return self._node_id_provider()
        return self.settings.node_id

    async def info(self) -> NodeReport:
        """返回节点信息（复用 collector，保证 RPC 查询与 $heartbeat 口径一致）。"""
        from llmpod.apps.node.collectors import collect_node_info

        report = await asyncio.to_thread(collect_node_info)
        report.node_id = self.node_id
        report.started = self._started
        report.ts = time.time()
        return report

    async def execute_shell(
        self,
        command: str,
        timeout: int = 60,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        """在节点上执行 shell 命令（独立 RPC）。"""
        from llmpod.apps.node.ops import execute_shell

        log.info("Executing shell: command=%r timeout=%s cwd=%s", command, timeout, cwd)
        try:
            return await execute_shell(command=command, timeout=timeout, cwd=cwd, env=env)
        except Exception as e:
            log.exception("Shell execution failed")
            return ShellResult(exit_code=-1, stdout="", stderr=str(e))

    async def docker_pull(self, image: str) -> AsyncIterator[str]:
        """流式拉取容器镜像（SDK 经 docker.sock——生产容器内无 docker CLI，不起命令子进程）。

        SDK chunk（``decode=True`` 的 dict）→ 可读文本行，wire 契约不变（逐行 NDJSON）。
        流的打开/消费/失败契约（流内 error chunk、流末落盘复核、迭代器关闭）统一在
        ``deployers.docker.iter_pull_chunks``（拉取流唯一消费口），本方法只做文本化。

        失败走异常（消费端 ``_stream_ndjson`` 转 ``{"event":"error"}`` 收尾，这是该
        路由文档化的失败通道）——产出 ``pull failed:`` 文本行会被当普通 data 帧、
        前端显示成功，是「拉取假成功」事故在 RPC 路径的翻版。
        """
        log.info("Pulling image (SDK): %s", image)
        try:
            async for chunk in iter_pull_chunks(image):
                yield _format_pull_chunk(chunk)
        except TaskExecutionError as e:
            raise RuntimeError(str(e)) from e
        log.info("Image pull stream complete: %s", image)

    async def image_exists(self, image: str) -> bool:
        """镜像存在性探测（底层 ``image_present_sync``）。

        只把「不存在」（ImageNotFound）塌缩成 False；daemon 错误原样抛——manager
        侧复核依赖异常区分「确不在位」与「探测通道不可用」：若 daemon 抖动也被塌
        缩成 False，在位镜像会被误判缺失并触发无谓重拉（连续数轮可耗尽重试上限）。
        PULL_IMAGE 前置检查用：Manager 先探测再决定是否下发 pull task，镜像已在位则
        直接记 SUCCESS（离线/无 registry 也能复用本地缓存镜像）。
        """
        return await asyncio.to_thread(image_present_sync, image)

    async def tail_log(self, container_name: str, lines: int = 20) -> AsyncIterator[str]:
        """追踪容器日志（docker SDK follow 长连接；中断干净可逆）。

        ``container.logs(follow=True, stream=True)`` 一次拿 backlog(tail=lines) + 持续跟随，
        游标由 docker 内部维护——免除时间戳/浮点/since 含闭边界等手工游标方案的历史坑
        （含闭边界去重集合会被清空 → 末行隔轮重发）。容器崩溃/重建（同名先清再起 → 新 log）
        对 follow 天然透明：流结束 → 重开回看新 backlog；同一容器 stop/restart 窗口不重 tail
        （``container.id`` 未变 → tail=0 只跟新行，不重复吐旧日志）。非 running 态（尚未创建/
        created/backoff/崩溃后）在复活窗口内降频重开并节流吐诊断行；首触即已停止（exited）的
        容器回看一次 backlog 即结束（刻意看历史）。

        中断（三条路径都在生成器 await 点干净退出，不留后台 follow 连接）：消费者断开
        （前端关面板/abort → HTTP 断 → starlette 关闭响应迭代 → 隧道发 $cancel → worker
        task.cancel → aclose 注入）；复活窗口超时（容器持续未回 running）；正常结束（首触
        历史/容器移除）。取消在 finally 里 ``gen.close()`` 干净关闭阻塞线程的 ``next()``
        （实测解阻塞无死锁），follow 连接不泄漏。
        """
        log.info("Tailing logs: container=%s, lines=%d", container_name, lines)
        last_running = time.monotonic()
        last_container_id: str | None = None
        first = True
        polls = 0
        try:
            while True:
                polls += 1
                try:
                    container = await asyncio.to_thread(get_docker_client().containers.get, container_name)
                except NotFound:
                    container = None
                status = container.status if container is not None else None
                if status == "running":
                    last_running = time.monotonic()

                if container is None:
                    # 未创建/已删（部署准备/重建窗口）：降频轮询 + 节流诊断，复活窗口内继续
                    if polls % _TAIL_DIAG_EVERY == 1:
                        yield f"[llmpod] 容器未运行（{_TAIL_WAIT_MESSAGE}），等待启动/重启/重建…"
                    if time.monotonic() - last_running > _TAIL_REVIVE_WINDOW_S:
                        return
                    await asyncio.sleep(_TAIL_MISSING_POLL_INTERVAL_S)
                    continue

                # 新容器（含首触）→ 回看 backlog；同一容器重开（stop/restart 窗口）→ 只跟新行
                fresh = container.id != last_container_id
                last_container_id = container.id

                if first and status == "exited":
                    # 首触即停止：回看一次 backlog 即结束（无 follow 语义，刻意看历史）
                    gen = await asyncio.to_thread(
                        container.logs, tail=lines, follow=False, stream=True, stdout=True, stderr=True
                    )
                    try:
                        while True:
                            chunk = await asyncio.to_thread(next_stream_chunk, gen)
                            if chunk is STREAM_DONE:
                                break
                            line = chunk.decode(errors="replace").rstrip("\n")
                            if line:
                                yield line
                    finally:
                        gen.close()
                    return

                # running（或已跟过后的重开）：backlog（新容器）+ 持续跟随；流结束=容器停/重建
                gen = await asyncio.to_thread(
                    container.logs, tail=lines if fresh else 0, follow=True, stream=True, stdout=True, stderr=True
                )
                first = False
                try:
                    while True:
                        chunk = await asyncio.to_thread(next_stream_chunk, gen)
                        if chunk is STREAM_DONE:
                            break
                        line = chunk.decode(errors="replace").rstrip("\n")
                        if line:
                            yield line
                finally:
                    gen.close()

                # 流结束（容器停/重建）：复活窗口内降频重开，超时结束
                if time.monotonic() - last_running > _TAIL_REVIVE_WINDOW_S:
                    log.info(
                        "tail_log %s: 复活窗口 %ds 内未回 running（state=%s），结束流",
                        container_name,
                        int(_TAIL_REVIVE_WINDOW_S),
                        status,
                    )
                    return
                await asyncio.sleep(_TAIL_POLL_INTERVAL_S)
        finally:
            # 结束可观测：正常结束/窗口超时/取消（CancelledError 传播）都经此——
            # 后台残留会在此现形（取消了还在 follow 的生成器不会走到这行日志）
            log.info("tail_log %s: ended (polls=%d)", container_name, polls)
