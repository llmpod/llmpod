"""TunnelServer — manager 端隧道服务器。

接受 worker 连接,管理节点,提供类型化代理。
与框架无关: 在任何 WebSocket 框架中使用 accept_worker(transport),
或使用 serve() 配合 websockets 库独立运行。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TypeVar

from .rpc_channel import RpcChannel
from .hub import Hub
from .registry import ServiceRegistry
from ..util.transport import Transport
from ..entity import protocol as P
from ..schemas import NodeReport

T = TypeVar("T")
log = logging.getLogger("llmpod.tunnel.server")


class TunnelServer:
    """Manager 端隧道服务器: 接受 worker 连接,管理节点,提供类型化代理。

    与框架无关 — 使用 accept_worker(transport) 集成到任意 WebSocket 框架,
    或使用 serve(host, port) 独立运行。
    """

    def __init__(self):
        self.hub = Hub()
        # 通道构造用的占位注册表：业务侧（manager bootstrap）在 $hello 后给每条
        # 通道换上绑定了鉴权节点身份的独立 registry，不在这全局共享注册。
        self._registry = ServiceRegistry()
        self._channels: list[RpcChannel] = []  # 跟踪活跃通道用于清理

    # ---- 节点管理 ----
    def list_nodes(self):
        return self.hub.list()

    def proxy(self, node_id: str, service_cls: type[T]) -> T:
        """返回类型化代理 — 像调用本地方法一样调用远程 worker。"""
        return self.hub.proxy(node_id, service_cls)

    # ---- 与框架无关: 接受传输层 ----
    async def accept_worker(self, transport: Transport, *, start: bool = True) -> RpcChannel:
        """接受 worker 连接,创建并启动 RpcChannel。

        调用方在此返回后负责等待 channel.wait_closed()。业务入口可传 start=False，
        在绑定鉴权回调后再启动读循环，消除默认 hello 回调抢先运行的窗口。
        """
        channel = RpcChannel(
            transport,
            registry=self._registry,
            name="server",
            on_hello=lambda params: self._on_hello(channel, params),
            on_heartbeat=lambda params: self._on_heartbeat(channel, params),
            on_close=lambda: self._on_close(channel),
        )
        self._channels.append(channel)
        if start:
            await channel.start()
        return channel

    # ---- 独立运行: websockets 库服务器 ----
    async def serve_channel(self, channel: RpcChannel) -> None:
        """通道生命周期兜底：等关闭后无条件 untrack。

        通道跟踪不挂在可被覆盖的 ``on_close`` 回调上——业务侧（manager bootstrap
        的 setup_callbacks）整体替换 on_close 时，框架自身的簿记必须仍然执行，
        否则被拒握手（$hello 校验前 close、node_id 恒空）的通道连对象带回调
        闭包常驻 _channels，重连循环下内存单调上涨。FastAPI 接入不走 serve()，
        由 NodeHub.serve_channel 转调同一兜底。
        """
        try:
            await channel.wait_closed()
        finally:
            self.untrack(channel)

    async def serve(self, host="0.0.0.0", port=8000, path="/ws/worker"):
        """运行独立 WebSocket 服务器(使用 websockets 库)。阻塞直至 Ctrl+C。"""
        import websockets
        from ..util.transport import WebsocketsTransport

        async def handler(ws):
            channel = await self.accept_worker(WebsocketsTransport(ws))
            await self.serve_channel(channel)

        log.info("TunnelServer 监听中 ws://%s:%d%s", host, port, path)
        async with websockets.serve(handler, host, port):
            await asyncio.Future()  # 永久运行

    # ---- 清理 ----
    async def close(self):
        """关闭所有活跃通道。"""
        for channel in list(self._channels):
            try:
                await channel.close()
            except Exception:
                pass
        self._channels.clear()

    # ---- Worker 生命周期回调(来自 master.py Master 类) ----
    async def _on_hello(self, channel: RpcChannel, params: dict):
        node_id = params.get("node_id") or f"node-{P.new_id()}"
        channel.node_id = node_id
        old = self.hub.get(node_id)
        if old is not None and old.channel is not channel:
            log.warning("node_id=%s 重复连接,踢掉旧通道", node_id)
            await old.channel.close()
        self.hub.register(node_id, channel, NodeReport.model_validate(params.get("info") or {}))
        log.info("worker 上线: %s", node_id)

    async def _on_heartbeat(self, channel: RpcChannel, params: dict):
        node = self.hub.get(channel.node_id) if channel.node_id else None
        if node:
            node.last_heartbeat = time.time()
            raw_info = params.get("info")
            if raw_info:
                node.info = NodeReport.model_validate(raw_info)

    async def _on_close(self, channel: RpcChannel):
        nid = getattr(channel, "node_id", None)
        if not nid:
            return
        node = self.hub.get(nid)
        if node is not None and node.channel is channel:
            self.hub.remove(nid)
            log.info("worker 离线: %s", nid)
        # 从跟踪的通道列表中移除
        self.untrack(channel)

    def untrack(self, channel: RpcChannel) -> None:
        """从 _channels 跟踪列表移除。

        on_close 回调可能被调用方（如 manager bootstrap）覆盖，导致默认 _on_close 不执行、
        _channels.remove 漏掉 → 重连循环下缓慢内存泄漏。暴露此方法供覆盖方显式清理。
        """
        try:
            self._channels.remove(channel)
        except ValueError:
            pass
