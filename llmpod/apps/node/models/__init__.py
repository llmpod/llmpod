"""Node 本地 ORM 模型包（SQLite，独立 metadata；与 manager 的 PG ``llmpod/models/`` 解耦）。

导入即注册到 ``NodeBase.metadata``——``TaskStore.init`` 的 ``create_all`` 据此建本地表。
"""

from llmpod.apps.node.models.local_task import LocalTask, NodeBase

__all__ = ["LocalTask", "NodeBase"]
