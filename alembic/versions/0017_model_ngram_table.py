"""model 表新增 ngram_table_gb：PLE n-gram 嵌入表大小（显存预算扣减项）。

Revision ID: 0017_model_ngram_table
Revises: 0016_node_source_merge
Create Date: 2026-11-21

Qwen4Exp 一族的模型把约一半权重体积放在 PLE n-gram 嵌入表里，而推理引擎
（vLLM）默认把该表放在 CPU 内存（cpu_offload 缺省开）或磁盘（disk_offload），
不进显存。旧口径按整份 size_gb 均摊显存，对这类模型高估约 2 倍，4×32G 卡
明明放得下却必须 force。新列由探测期扫 safetensors 头填入；存量行为空 =
不扣减（即修复前口径，不因迁移改变判定）。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_model_ngram_table"
down_revision: Union[str, None] = "0016_node_source_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model",
        sa.Column(
            "ngram_table_gb",
            sa.Float(),
            nullable=True,
            comment="n-gram 嵌入表大小（GB，Qwen4Exp PLE 表；推理引擎默认驻留内存/磁盘不进显存，显存预算= size_gb − 本值，可空=未扫描）",
        ),
    )


def downgrade() -> None:
    op.drop_column("model", "ngram_table_gb")
