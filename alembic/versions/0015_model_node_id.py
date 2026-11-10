"""model 表新增 node_id：来源节点目录就地引用。

Revision ID: 0015_model_node_id
Revises: 0014_model_kv_layers
Create Date: 2026-11-09

``source=node`` 的模型权重在指定节点的目录上就地引用，manager 没有副本，也不能自动
下载。node_id 记录绑定的节点，部署时据此限制目标节点。存量行为空，行为不变。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_model_node_id"
down_revision: Union[str, None] = "0014_model_kv_layers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model",
        sa.Column(
            "node_id",
            sa.String(length=64),
            nullable=True,
            comment="来源节点ID（逻辑外键 → node.id；source=node 时绑定该节点，权重就地引用）",
        ),
    )
    op.create_index("idx_model_node_id", "model", ["node_id"])


def downgrade() -> None:
    op.drop_index("idx_model_node_id", table_name="model")
    op.drop_column("model", "node_id")
