"""balance_hold.api_key_id 注释随平台 Key 预算拦截语义更新。

Revision ID: 0008_balance_hold_key_comment
Revises: 0007_upstream_name_comments
Create Date: 2026-09-29

平台直发 Key（tenant_id 为空）开始参与 Key 预算冻结（budget_frozen 原子占位，
budget_limit NULL=不限），但仍不写 balance_hold 行（该表 tenant_id NOT NULL，
平台 Key 无钱包冻结可记）。api_key_id 列注释里「平台 Key 无冻结，恒 NULL」的
说法只剩「无钱包冻结账行」一层含义，且表本身只记钱包冻结——注释改为直接说明
「无钱包冻结不落行」，避免被读成平台 Key 全程无任何冻结。存量库 alembic
upgrade 不会自动带上 comment 改动，单独以 alter_column 落注释。
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_balance_hold_key_comment"
down_revision: Union[str, None] = "0007_upstream_name_comments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "balance_hold",
        "api_key_id",
        comment="触发 Key（逻辑外键 → api_key.id）；本表只记钱包冻结，平台 Key 无钱包冻结、不落行",
    )


def downgrade() -> None:
    op.alter_column(
        "balance_hold",
        "api_key_id",
        comment="触发 Key（平台 Key 无冻结，恒 NULL）",
    )
