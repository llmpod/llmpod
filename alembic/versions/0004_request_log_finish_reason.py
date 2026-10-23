"""request_log 新增 finish_reason：上游结束原因原样落账。

Revision ID: 0004_request_log_finish_reason
Revises: 0003_request_log_ttft
Create Date: 2026-09-15

背景：大屏的「截断率」此前读的是 ``request_log.truncated``——那是**日志快照**
是否被截断 body（32KB/字段上限）的标记，与「生成被输出上限截断」是两码事
（它还会随 ``max_field_bytes`` 配置变化）。真正的信号是上游响应里的结束原因：
openai ``finish_reason == "length"`` / anthropic ``stop_reason == "max_tokens"``。
本列原样存该值（不归一），判定口径由查询侧一处翻译。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_request_log_finish_reason"
down_revision: Union[str, None] = "0003_request_log_ttft"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "request_log",
        sa.Column(
            "finish_reason",
            sa.String(length=32),
            nullable=True,
            comment="上游结束原因原样存（openai finish_reason / anthropic stop_reason；length/max_tokens = 被输出上限截断）",
        ),
    )


def downgrade() -> None:
    op.drop_column("request_log", "finish_reason")
