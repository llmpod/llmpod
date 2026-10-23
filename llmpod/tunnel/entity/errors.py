"""RPC 异常实体: RemoteError + NodeOffline。

RemoteError 封装 worker 端的失败,供 master 使用。
NodeOffline 在目标节点未连接时由类型化代理抛出。
"""

from __future__ import annotations

from .protocol import SERVER_ERROR


class RemoteError(Exception):
    """Worker 端调用失败,包含 code (JSON-RPC 错误码) 和 tb (traceback)。"""

    def __init__(self, message: str, tb: str = "", code: int = SERVER_ERROR):
        super().__init__(message)
        self.message = message
        self.tb = tb
        self.code = code


class NodeOffline(Exception):
    """目标节点未在线(当通道不可用时由类型化代理抛出)。"""
