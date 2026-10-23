"""传输层抽象: 显式 Protocol + WebSocket 库适配器。

Transport Protocol: 任何具有 send_text/receive_text/close 方法的对象。
FastAPI WebSocket 原生满足此 Protocol(无需适配器)。
WebsocketsTransport: 适配 websockets 库连接。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """与框架无关的 WebSocket 连接传输层接口。"""

    async def send_text(self, data: str) -> None:
        """发送文本帧。"""
        ...

    async def receive_text(self) -> str:
        """接收文本帧。"""
        ...

    async def close(self) -> None:
        """关闭连接。"""
        ...


class WebsocketsTransport:
    """websockets 库连接对象适配器(客户端或服务端)。"""

    def __init__(self, ws):
        self.ws = ws

    async def send_text(self, data: str):
        await self.ws.send(data)

    async def receive_text(self) -> str:
        msg = await self.ws.recv()
        return msg.decode() if isinstance(msg, bytes) else msg

    async def close(self):
        await self.ws.close()
