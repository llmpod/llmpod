"""node 表新增 net_ifname 列（持有 ip 的物理网卡名）。

Revision ID: 0009_node_net_ifname
Revises: 0008_balance_hold_key_comment
Create Date: 2026-10-09

多机部署向引擎容器注入 NCCL/GLOO 两个 SOCKET_IFNAME，此前取值硬编码兜底
eth0——systemd 可预测网卡名时代 enp3s0/ens/eno 才是常态，猜错即 gloo 找不到
接口、引擎 exit 1。节点采集器随 $hello 上报「持有 Node.ip 的网卡名」落此列，
部署组装按实例调度结果取用；空 = 节点未上报（未升级节点），消费方回落用户显式
配置与代码兜底。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_node_net_ifname"
down_revision: Union[str, None] = "0008_balance_hold_key_comment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "node",
        sa.Column(
            "net_ifname",
            sa.String(length=32),
            nullable=True,
            comment="持有 ip 的物理网卡名（多机分布式通信 SOCKET_IFNAME 注入用；空=节点未上报）",
        ),
    )


def downgrade() -> None:
    op.drop_column("node", "net_ifname")
