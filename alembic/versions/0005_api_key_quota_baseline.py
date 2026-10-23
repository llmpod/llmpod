"""api_key 加配额重置基线列：rebuild_quota_counters 按净增量聚合。

背景：reset_budget 只清 PG budget_spent 与 Redis 两档 Key 维度计数器，
request_log 调用日志不动。网关启动的配额重建从调用日志全量聚合并「就高回填」，
重置会在 marker 过期/Redis 清库后的首次重建中被静默撤销。加
``quota_baselines``（按模型分桶的 JSONB）在重置时定格累计用量，重建聚合
按「全量 − 基线」得出重置后净增量——调用日志行一行不删，重置语义可恢复。

Revision ID: 0005_api_key_quota_baseline
Revises: 0004_request_log_finish_reason
Create Date: 2026-09-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_api_key_quota_baseline"
down_revision: Union[str, None] = "0004_request_log_finish_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_key",
        sa.Column(
            "quota_baselines",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="配额重置基线（token，按模型分桶）：reset_budget 定格累计用量，配额重建按净增量聚合",
        ),
    )


def downgrade() -> None:
    op.drop_column("api_key", "quota_baselines")
