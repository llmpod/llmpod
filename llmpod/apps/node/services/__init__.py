"""节点 RPC 服务实现包。

按契约拆分为 3 个服务（对应 ``tunnel.contract`` 的 IDL）：
- ``ContainerServiceImpl``: 容器生命周期 (deploy/stop/remove/list/inspect)
- ``NodeServiceImpl``: 节点级操作 (info/execute_shell/docker_pull/tail_log)
- ``TaskServiceImpl``: 声明式任务 (ensure_task/cancel_task) ——委托 NodeAgent

``ContainerServiceImpl`` 委托 Deployer（docker/k8s 可插拔）。
``TaskServiceImpl`` 委托 NodeAgent（持有 local_tasks 持久化 + executor 编排）。
"""

from llmpod.apps.node.services.container import ContainerServiceImpl
from llmpod.apps.node.services.node import NodeServiceImpl
from llmpod.apps.node.services.task import TaskServiceImpl

__all__ = [
    "ContainerServiceImpl",
    "NodeServiceImpl",
    "TaskServiceImpl",
]
