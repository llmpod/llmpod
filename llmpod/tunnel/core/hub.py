"""Hub + 类型化代理系统(master 端) — 核心层。

Hub: 维护所有 worker 长连接,提供类型化代理。
hub.proxy(id, NodeService) 返回 ServiceProxy,其 .method 触发
MethodProxy.__call__ → channel.call_unary/stream,使远程调用
看起来像本地方法调用(同时从抽象类获得 IDE 自动补全)。
"""

from __future__ import annotations

from typing import TypeVar

from ..entity.errors import NodeOffline
from ..entity.node_info import NodeInfo
from ..schemas.system import NodeReport
from ..util.reflect import build_params, class_metadata, coerce_wire

T = TypeVar("T")


class MethodProxy:
    """单个方法的可调用代理。一元返回可等待对象,流式返回异步生成器。

    一元调用的 wire dict 按 IDL 返回类型注解经 ``model_validate`` 归一为
    Pydantic model 实例(调用方拿到强类型对象);流式原样透传。
    """

    def __init__(self, getter, service: str, method: str, is_stream: bool, param_names: list[str], return_type=None):
        self._getter = getter
        self.service = service
        self.method = method
        self.is_stream = is_stream
        self._param_names = param_names
        self._return_type = return_type

    def __call__(self, *args, _rpc_timeout: float | None = None, **kwargs):
        """按 IDL 参数名将位置参数映射为 params 对象,然后调用 channel.call_unary 或 stream。

        ``_rpc_timeout`` 是一元调用专属的传输层超时（秒），不进 params——超时经
        ``channel.call_unary`` 转 ``RemoteError(TIMEOUT)`` 并向 node 发 ``$cancel``。
        刻意用下划线前缀避免与 IDL 参数名撞车（如 ``execute_shell`` 自带 timeout 参数）。
        """
        ch = self._getter()
        if ch is None:
            raise NodeOffline("节点未连接")
        params = build_params(self._param_names, args, kwargs)
        if self.is_stream:
            return ch.stream(self.service, self.method, params)
        return self._call_and_coerce(ch, params, _rpc_timeout)

    async def _call_and_coerce(self, ch, params, _rpc_timeout: float | None = None):
        """一元调用 + 按 return_type 把 wire dict 归一为 Pydantic model。"""
        result = await ch.call_unary(self.service, self.method, params, timeout=_rpc_timeout)
        return _coerce(result, self._return_type)


def _coerce(result, return_type):
    """按 IDL 返回类型注解把 wire dict/list 归一为 Pydantic model / model 列表。

    归一逻辑与 worker 入参方向共用 ``reflect.coerce_wire``（单一实现）；此处再包一层
    「校验失败原样返回」的向后兼容语义。
    """
    try:
        return coerce_wire(result, return_type)
    except Exception:
        return result


class ServiceProxy:
    """单个服务的类型化代理。方法元数据来自抽象类(IDL)。"""

    def __init__(self, getter, service: str, method_meta: dict[str, dict]):
        self._getter = getter
        self.service = service
        self._meta = method_meta

    def __getattr__(self, name: str) -> MethodProxy:
        """按方法名创建 MethodProxy,元数据包含 is_stream/参数名。"""
        if name.startswith("_"):
            raise AttributeError(name)
        meta = self._meta.get(name, {})
        return MethodProxy(
            self._getter, self.service, name, meta.get("stream", False), meta.get("params", []), meta.get("return")
        )


class Hub:
    """Master 端节点枢纽: 维护所有长连接,提供类型化代理。"""

    def __init__(self):
        self._nodes: dict[str, NodeInfo] = {}

    def register(self, node_id: str, channel, info: NodeReport) -> NodeInfo:
        """注册 worker 连接(在 on_hello 时调用)。"""
        node = NodeInfo(node_id, channel, info)
        self._nodes[node_id] = node
        return node

    def get(self, node_id: str) -> NodeInfo | None:
        """按 node_id 获取节点(未在线时返回 None)。"""
        return self._nodes.get(node_id)

    def remove(self, node_id: str):
        """移除节点(在 on_close 时调用)。"""
        self._nodes.pop(node_id, None)

    def list(self):
        """列出所有在线节点。"""
        return list(self._nodes.values())

    def proxy(self, node_id: str, service_cls: type[T]) -> T:
        """返回类型化代理: IDE 可从抽象类自动补全/导航。"""
        svc = service_cls.__name__
        meta = class_metadata(service_cls)
        hub, nid = self, node_id

        def getter():
            n = hub.get(nid)
            return n.channel if n else None

        return ServiceProxy(getter, svc, meta)  # type: ignore
