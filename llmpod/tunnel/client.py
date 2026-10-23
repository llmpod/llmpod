"""TunnelClient — Node 端隧道客户端。

从 ws_tunnel Worker 重构。嵌入 llmpod/tunnel/ 包内。
通过 WebSocket 连接 Manager,注册服务,响应 RPC 调用,
分层退避重连,并管理 $register_ack 以实现 node_id 持久化。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

import websockets

from .core.rpc_channel import RpcChannel
from .core.registry import ServiceRegistry
from .schemas.system import NodeReport
from .util.transport import WebsocketsTransport

log = logging.getLogger("llmpod.tunnel.node")


class TunnelClient:
    """Node 端隧道客户端: 反向连接 TunnelServer + 注册服务
    + 响应调用 + 分层重连。

    node_id: 首次启动时为空,Manager 通过 $register_ack 发送 UUID,Node 持久化。
    token/cluster_id: 握手请求头与 $hello 携带，不进入 URL。
    info_collector: 节点信息采集回调,返回 $hello/$heartbeat 携带的 info 字典。
        采集器实现由 apps/node 层注入(系统/GPU/容器等指标),保持 tunnel 层与业务无关。
    """

    def __init__(
        self,
        master_url: str,
        node_id: str = "",
        cluster_id: int = 0,
        token: str = "",
        bootstrap_token: str = "",
        registry: ServiceRegistry | None = None,
        node_id_file: str = "data/node_id",
        heartbeat_interval: int = 10,
        reconnect_fast_interval: int = 5,
        reconnect_slow_interval: int = 30,
        reconnect_fast_window: int = 60,
        hello_ack_timeout: float = 60.0,
        info_collector: Callable[[], NodeReport] | None = None,
        notification_handlers: dict[str, Callable[[dict], Awaitable[None]]] | None = None,
        on_registered: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.master_url = master_url
        self.node_id = node_id  # 首次启动时为空,$register_ack 后填充
        self.cluster_id = cluster_id
        self.token = token
        self.bootstrap_token = bootstrap_token
        self.registry = registry or ServiceRegistry()
        self.node_id_file = node_id_file
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_fast_interval = reconnect_fast_interval
        self.reconnect_slow_interval = reconnect_slow_interval
        self.reconnect_fast_window = reconnect_fast_window
        # $hello 发出后等 $register_ack 的上限：manager 短暂不可用（依赖重启/半开连接）时
        # 无 ack 也要主动断开进重连退避，绝不能停在「已发 hello 未注册」状态干等
        self.hello_ack_timeout = hello_ack_timeout
        self.info_collector = info_collector  # 由 apps/node 注入;为空时仅上报基础信息
        # 扩展通知 handler（$node_upgrade / $node_decommission / $register_nack 等，
        # method → coroutine）。channel 每次重连重建，_serve 内重新绑定。
        self.notification_handlers = dict(notification_handlers or {})
        # $register_ack 之后的回调（node_id 就绪，每次重连注册成功都会触发）——
        # apps/node 用于写 ready marker / 触发升级后对账等。
        self.on_registered = on_registered
        self.channel: RpcChannel | None = None
        self._connect_time: float | None = None
        # $hello ack 看门狗任务（每次 _serve 重建；ack 到达或 channel 关闭即取消）
        self._ack_watchdog: asyncio.Task | None = None
        # 上一帧采集任务（超时弃等后线程可能还卡着，按 done() 判 in-flight）：
        # in-flight 期间直接回退基础信息，不再往线程池叠加新任务——卡死的采集器
        # 会耗尽默认线程池，连带镜像拉取等所有 to_thread 调用排队停摆
        self._collect_task: asyncio.Task | None = None

    async def run(self):
        """主循环: 连接 → 运行 → 断开 → 分层重连。"""
        while True:
            try:
                await self._serve()
            except Exception as e:
                log.warning("连接错误: %r", e)

            # 分层重连
            elapsed = time.time() - self._connect_time if self._connect_time else 0
            if elapsed < self.reconnect_fast_window:
                delay = self.reconnect_fast_interval + random.uniform(0, 2)
            else:
                delay = self.reconnect_slow_interval + random.uniform(0, 5)

            log.info("%.1f 秒后重连...", delay)
            await asyncio.sleep(delay)

    async def _serve(self):
        """单次连接生命周期。"""
        self._connect_time = time.time()
        log.info("连接 manager: %s", self.master_url)

        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-LLMPOD-Cluster-ID": str(self.cluster_id),
        }
        if self.node_id:
            headers["X-LLMPOD-Node-ID"] = self.node_id
        else:
            headers["X-LLMPOD-Bootstrap-Token"] = self.bootstrap_token
        async with websockets.connect(self.master_url, max_size=2**24, additional_headers=headers) as ws:
            transport = WebsocketsTransport(ws)
            self.channel = RpcChannel(
                transport,
                self.registry,
                name=f"node:{self.node_id or 'pending'}",
                on_register_ack=self._on_register_ack,
            )
            for method, handler in self.notification_handlers.items():
                self.channel.on_notification(method, handler)
            await self.channel.start()

            # 发送 $hello 通知(首次 node_id 为空,Manager 生成 UUID)
            hello_params: dict = {
                "token": self.token,
                "cluster_id": self.cluster_id,
                "info": await self._collect_info_async(),
            }
            if self.node_id:
                hello_params["node_id"] = self.node_id
            else:
                hello_params["bootstrap_token"] = self.bootstrap_token
            await self.channel.send_notification("$hello", hello_params)
            log.info(
                "已发送 $hello (cluster_id=%s, node_id=%s)", self.cluster_id, self.node_id or "(首次,等待 Manager 分配)"
            )

            # hello ack 看门狗：$register_ack 到达即取消（见 _on_register_ack）；超时说明
            # manager 侧不可用（依赖重启/半开连接），主动断开交给主循环走重连退避——
            # 否则进程会停在「已发 hello 未注册」状态无限等待，只能重启恢复。
            self._ack_watchdog = asyncio.create_task(self._hello_ack_watchdog())

            hb = asyncio.create_task(self._heartbeat())
            try:
                await self.channel.wait_closed()
            finally:
                # ack 正常到达时 _on_register_ack 已把 _ack_watchdog 置 None，
                # 此处按当下值收尾（无条件 cancel 会在 None 上抛 AttributeError，
                # 打断清理：channel 不清空、真实断连原因被掩盖）
                tasks = [t for t in (self._ack_watchdog, hb) if t is not None]
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    try:
                        await t
                    except asyncio.CancelledError:
                        # 外层自身被取消时把取消透传出去——吞掉会让 _serve 正常
                        # 返回、run() 误以为可重连，关机取消失效
                        if asyncio.current_task().cancelled():
                            raise
                    except Exception:
                        pass
                self._ack_watchdog = None
            self.channel = None

    async def _hello_ack_watchdog(self) -> None:
        """$hello 发出后 ``hello_ack_timeout`` 秒内未见 $register_ack → 断开重连。

        ack 到达由 :meth:`_on_register_ack` 取消本任务；channel 关闭时本任务也被
        外层 finally 取消，双保险不悬挂。
        """
        await asyncio.sleep(self.hello_ack_timeout)
        log.warning("$hello 后 %.0fs 未收到 $register_ack（manager 不可用？），断开重连", self.hello_ack_timeout)
        if self.channel is not None:
            await self.channel.close()

    async def _on_register_ack(self, params: dict):
        """处理 Manager 的 $register_ack: 持久化 node_id 以供重启复用。"""
        # ack 已到：撤 hello 看门狗，连接进入正常服务态
        if self._ack_watchdog is not None:
            self._ack_watchdog.cancel()
            self._ack_watchdog = None
        node_id = params.get("node_id")
        if node_id and node_id != self.node_id:
            self.node_id = node_id
            from pathlib import Path

            p = Path(self.node_id_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(node_id)
            log.info("已持久化 Manager 分配的 node_id=%s → %s", node_id, self.node_id_file)
        if self.on_registered is not None and node_id:
            try:
                await self.on_registered(str(node_id))
            except Exception:
                log.exception("on_registered callback error")

    async def _heartbeat(self):
        """定期发送 $heartbeat 通知,携带状态负载(替代 report_status RPC)。

        info 中由 info_collector 提供 CPU/GPU/内存等运行时指标,
        Manager 在 on_heartbeat 中更新 NodeInfo 与 GPU 状态缓冲。
        """
        while True:
            await asyncio.sleep(self.heartbeat_interval + random.uniform(-1, 1))
            if self.channel is None or self.channel.closed:
                return
            try:
                await self.channel.send_notification(
                    "$heartbeat",
                    {
                        "node_id": self.node_id,
                        "info": await self._collect_info_async(),
                    },
                )
            except Exception as e:
                log.warning("心跳发送失败: %r", e)
                return

    def _collect_info(self) -> NodeReport:
        """收集节点信息,用于 $hello/$heartbeat 的 info payload。

        默认仅返回基础字段;由 apps/node 层通过 info_collector 注入完整采集器
        (hostname/ip/cpu/gpus/内存/容器等运行态)。采集失败时回退到基础信息,
        保证心跳不因采集异常而中断。

        注意:此方法会同步执行 info_collector(可能包含 GPU/系统采集,耗时
        较长,尤其 Ascend 需逐卡走 DCMI 查询)。在 event loop 线程内应改用
        :meth:`_collect_info_async` 避免阻塞。
        """
        if self.info_collector is not None:
            try:
                return self.info_collector()
            except Exception as e:
                log.warning("info_collector 调用失败,回退基础信息: %r", e)
        return self._base_report()

    @staticmethod
    def _base_report() -> NodeReport:
        """无采集器/采集失败/采集超时共用的兜底：仅节点名称的基础报告。"""
        import socket

        from llmpod.config.settings import settings

        return NodeReport(hostname=settings.node_name or socket.gethostname())

    async def _collect_info_async(self, timeout: float = 30.0) -> NodeReport:
        """异步采集节点信息。

        把同步 :meth:`_collect_info` 放到线程池执行,避免在 event loop 线程
        内阻塞(Ascend 采集每卡 ~450ms,N 张卡可达数秒)。整体套 ``timeout``：
        采集器卡死（挂死的挂载点、驱动调用不返回）时放弃本帧回退基础信息，
        而不是让 $hello/心跳永远停在采集上——$hello 发不出去则注册看门狗
        根本不会建立，连接呈现「连着但没注册」。

        上一帧仍 in-flight（超时弃等但采集线程未返回）时不再叠新任务：卡死的
        采集器每 10s 心跳再塞一个永不返回的线程，很快占满默认线程池，同进程
        所有 to_thread（镜像拉取/容器操作）跟着排队停摆。超时用 ``shield`` 保住
        任务本体——done() 反映的是线程真实完工，弃等不清位，线程返回即自愈。
        """
        if self._collect_task is not None and not self._collect_task.done():
            log.warning("上一帧采集仍在途（采集器疑似卡死），本帧回退基础信息")
            return self._base_report()
        task = asyncio.create_task(asyncio.to_thread(self._collect_info))
        self._collect_task = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("info_collector 采集超时(%.0fs)，本帧回退基础信息", timeout)
            return self._base_report()


__all__ = ["TunnelClient"]
