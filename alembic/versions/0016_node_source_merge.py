"""节点目录模型按名合并：node 源同名唯一索引 + node_id 列改为溯源展示。

Revision ID: 0016_node_source_merge
Revises: 0015_model_node_id
Create Date: 2026-11-16

同名节点目录导入不再各建一条 model 行，而是合并到既有行、往 node_model 追加副本
（部署判据随之从「绑定节点」改为「持有 present 副本的节点集合」）。本迁移：

- 建 partial unique index ``uq_model_node_name``（name 唯一，仅 source='node' 行），
  兜底并发导入，create 路由撞索引时走合并分支；
- model.node_id 列注释改为「首次导入节点（溯源展示）」——副本权威清单在 node_model。

存量库若已有同名 node 来源模型行，索引建不起来：开发期 ``make db-reset`` 重建；
确需保留数据的先人工清理重名行再升级。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_node_source_merge"
down_revision: Union[str, None] = "0015_model_node_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "model",
        "node_id",
        comment="首次导入节点ID（逻辑外键 → node.id；source=node 时记录首个导入来源，展示溯源用；副本清单以 node_model 表为准）",
    )
    op.create_index(
        "uq_model_node_name",
        "model",
        ["name"],
        unique=True,
        postgresql_where=sa.text("source = 'node'"),
    )


def downgrade() -> None:
    op.drop_index("uq_model_node_name", table_name="model")
    op.alter_column(
        "model",
        "node_id",
        comment="来源节点ID（逻辑外键 → node.id；source=node 时绑定该节点，权重就地引用）",
    )
