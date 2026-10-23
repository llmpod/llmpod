"""JSON-RPC 2.0 协议帧构造器,带流式扩展。

线路协议: 基础帧遵循 JSON-RPC 2.0 (jsonrpc/method/params/result/error/id),
流式使用扩展字段 stream:true 标记后续帧。
通知无 id: $hello/$heartbeat/$cancel/$register_ack。
通过 id 字段实现多路复用 — 一个 WebSocket 上支持多个并发调用/流。

通过字段语义区分帧类型(无 type 字段):
  - 有 method → 请求(有 id) / 通知(无 id)
  - 有 result/error → 响应
  - method 使用 "Service.method" 点分命名空间; params 始终为对象
"""

from __future__ import annotations

import uuid

JSONRPC = "2.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERR = -32700
INVALID_REQ = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL = -32603

# 实现定义的错误码 (-32000..-32099)
SERVER_ERROR = -32000
NODE_OFFLINE = -32001
TIMEOUT = -32002
STREAM_CANCELLED = -32003
CHANNEL_CLOSED = -32004


def new_id() -> str:
    """生成唯一的调用 id (uuid hex[:12])，用于请求-响应匹配。"""
    return uuid.uuid4().hex[:12]


def request(method: str, params: dict, id: str) -> dict:
    """调用请求，带 id，期望响应。Method 使用 'Service.method' 命名空间。"""
    return {"jsonrpc": JSONRPC, "method": method, "params": params, "id": id}


def notification(method: str, params: dict) -> dict:
    """通知（无 id，不期望响应）。$ 前缀的方法是扩展。"""
    return {"jsonrpc": JSONRPC, "method": method, "params": params}


def success(id: str, result, stream: bool = False) -> dict:
    """成功响应。stream=True 标记后续帧；默认为最终帧。"""
    f = {"jsonrpc": JSONRPC, "result": result, "id": id}
    if stream:
        f["stream"] = True
    return f


def stream_frame(id: str, value) -> dict:
    """流式帧: result 为当前数据，stream:true 表示后续还有数据。"""
    return {"jsonrpc": JSONRPC, "result": value, "id": id, "stream": True}


def stream_end(id: str) -> dict:
    """流式结束帧: result 为 null，无 stream 字段表示最终帧。"""
    return {"jsonrpc": JSONRPC, "result": None, "id": id}


def error(id: str, code: int, message: str, data=None) -> dict:
    """错误响应: error 包含 {code, message, data?}。data 通常携带 traceback。"""
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": JSONRPC, "error": e, "id": id}
