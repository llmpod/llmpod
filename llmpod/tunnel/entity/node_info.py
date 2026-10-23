"""节点信息实体: manager 端维护的每个 worker 连接状态。

Hub 使用此类注册每个在线 worker; on_hello 创建,
on_heartbeat 更新 last_heartbeat, on_close 移除。
"""

from __future__ import annotations

from ..schemas.system import NodeReport


class NodeInfo:
    """Manager 端的 worker 连接注册信息: node_id, channel, info, last_heartbeat。"""

    def __init__(self, node_id: str, channel, info: NodeReport):
        self.node_id = node_id
        self.channel = channel
        self.info = info
        self.last_heartbeat: float | None = None
