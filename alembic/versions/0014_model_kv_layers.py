"""model 表新增 kv_layers：有 KV cache 的层数（混合线性注意力模型的 KV 估算输入）。

Revision ID: 0014_model_kv_layers
Revises: 0013_comment_backfill
Create Date: 2026-10-29

Qwen3.5 这类混合线性注意力模型（full_attention_interval=4）只有 1/4 的层存 KV，
KV 估算按全层算会高估数倍，把本该放行的部署误拒。新列在模型探测时从 config 的
``layer_types`` / ``full_attention_interval`` 推出；存量行为空，估算回退按 num_layers
（即修复前的口径，不会因迁移本身改变判定）。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_model_kv_layers"
down_revision: Union[str, None] = "0013_comment_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model",
        sa.Column(
            "kv_layers",
            sa.Integer(),
            nullable=True,
            comment="有 KV cache 的层数（混合线性注意力模型少于层数；KV 估算用，空=按层数）",
        ),
    )


def downgrade() -> None:
    op.drop_column("model", "kv_layers")
