"""request_log 新增 ttft_ms：首帧延迟逐请求落账。

Revision ID: 0003_request_log_ttft
Revises: 0002_metric_engine_rollup
Create Date: 2026-09-15

背景：端到端延迟（latency_ms）已有，但「吐字速度」= 输出 token / 生成耗时
需要逐请求的 (latency − ttft) 分母；首帧延迟此前只有聚合值（proxy 的 ttft
直方图 + rollup 的 ttft_sum_ms/ttft_count），减不出吞吐。仅流式有值——非流式
拿不到「首帧」这个时点，置空而不是拿总延迟冒充。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_request_log_ttft"
down_revision: Union[str, None] = "0002_metric_engine_rollup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "request_log",
        sa.Column(
            "ttft_ms",
            sa.Integer(),
            nullable=True,
            comment="首帧延迟（毫秒；仅流式有值，非流式拿不到首帧时点）",
        ),
    )


def downgrade() -> None:
    op.drop_column("request_log", "ttft_ms")
