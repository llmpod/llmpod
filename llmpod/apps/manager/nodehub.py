"""NodeHub — Manager 端隧道入口。

TunnelServer 的业务语义封装。Manager 使用 NodeHub
而非直接使用 TunnelServer,以保持清晰的业务意图。
"""

from __future__ import annotations

from fastapi import WebSocket

from llmpod.tunnel.core.rpc_channel import RpcChannel
from llmpod.tunnel.core.server import TunnelServer
from llmpod.tunnel.schemas.system import NodeReport


class NodeHub:
    """Manager 端节点枢纽: 业务语义封装,内部使用 TunnelServer。"""

    def __init__(self):
        self._server = TunnelServer()

    @property
    def hub(self):
        """暴露底层 Hub,供节点查询/管理。"""
        return self._server.hub

    @property
    def server(self):
        """暴露底层 TunnelServer,供高级用法使用。"""
        return self._server

    async def accept(self, websocket: WebSocket) -> RpcChannel:
        """接受 Node 的 WebSocket 连接。"""
        await websocket.accept()
        return await self._server.accept_worker(websocket, start=False)

    def proxy(self, node_id: str, service_cls):
        """返回指定 Node 的类型化代理。"""
        return self._server.proxy(node_id, service_cls)

    async def serve_channel(self, channel: RpcChannel) -> None:
        """通道生命周期兜底（等关闭 + 无条件 untrack）。

        FastAPI 接入不走 ``TunnelServer.serve``，ws 端点需自调同一兜底——通道
        跟踪不挂在可被业务覆盖的 on_close 回调上（被拒握手通道才不泄漏）。
        """
        await self._server.serve_channel(channel)

    def get(self, node_id: str):
        """查询节点在线信息。"""
        return self._server.hub.get(node_id)

    def register(self, node_id: str, channel, info: NodeReport):
        """在枢纽中注册节点。"""
        return self._server.hub.register(node_id, channel, info)

    def remove(self, node_id: str):
        """从枢纽中移除节点。"""
        self._server.hub.remove(node_id)

    async def close(self):
        """关闭所有活跃通道。"""
        await self._server.close()
