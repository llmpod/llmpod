"""LLMPod Tunnel: WebSocket RPC 通信层。

与框架无关的反向隧道 RPC 库（JSON-RPC 2.0 + 流式扩展）。
从 ws_tunnel 项目重构并嵌入 llmpod。

本包仅提供与框架无关的传输/协议原语（TunnelClient / TunnelServer /
RpcChannel / contract IDL / schemas）。Manager 端的业务语义封装
``NodeHub`` 位于 ``llmpod.apps.manager.nodehub``——tunnel 包刻意不含业务层。

快速开始:
  # Node 端（无 FastAPI 依赖）
  from llmpod.tunnel import TunnelClient
  from llmpod.tunnel.core.registry import ServiceRegistry
  registry = ServiceRegistry()
  registry.register(NodeServiceImpl(), "NodeService")
  client = TunnelClient("ws://manager:8080/ws/node", registry=registry)
  await client.run()

结构:
  client.py     - TunnelClient (Node 端入口, 无 FastAPI 依赖)
  core/         - 通用 RPC 核心 (registry/server/rpc_channel/hub)
  entity/       - 数据模型 (node_info/errors/protocol)
  util/         - 辅助 (transport/reflect)
  contract.py   - RPC 服务契约 (抽象 IDL) — 无业务实现
"""

from .client import TunnelClient
from .core.rpc_channel import RpcChannel
from .core.hub import Hub, ServiceProxy, MethodProxy
from .core.registry import ServiceRegistry
from .core.server import TunnelServer
from .util.transport import Transport, WebsocketsTransport
from .contract import ContainerService, NodeService
from .entity.errors import RemoteError, NodeOffline
from .entity.node_info import NodeInfo

__all__ = [
    "TunnelClient",
    "TunnelServer",
    "RpcChannel",
    "Hub",
    "ServiceProxy",
    "MethodProxy",
    "ServiceRegistry",
    "Transport",
    "WebsocketsTransport",
    "ContainerService",
    "NodeService",
    "RemoteError",
    "NodeOffline",
    "NodeInfo",
]
