"""回退 per-node 凭据后保留 node-auth 版本号占位。

Revision ID: 0010_node_auth_token
Revises: 0009_node_net_ifname

后续 revision 的链式依赖仍指向本 ID。已应用过原 revision 的存量库可能残留
一个未使用的可空 node.auth_token 列，ORM 层忽略它，无行为影响。
"""

from __future__ import annotations

from typing import Sequence, Union

revision: str = "0010_node_auth_token"
down_revision: Union[str, None] = "0009_node_net_ifname"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
