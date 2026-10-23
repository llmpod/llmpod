"""request_log 新增 route_reason：本次请求为什么落到这个上游。

Revision ID: 0006_request_log_route_reason
Revises: 0005_api_key_quota_baseline
Create Date: 2026-09-16

背景：`endpoint_id` 只说落到哪个上游，不说为什么。排查「这个会话怎么被甩到别的
供应商去了」时，只能靠 `retry_count` 猜是不是故障换端，看不出是会话亲和（缓存优先）
还是负载均衡（优先级 + 权重）选的。本列把选路出处显式落账——触发时机（首次选路 /
失败换端）× 选出机制（会话亲和 / 优先级+权重）的 2×2，取值见 ORM 列注释。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_request_log_route_reason"
down_revision: Union[str, None] = "0005_api_key_quota_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "request_log",
        sa.Column(
            "route_reason",
            sa.String(length=32),
            nullable=True,
            comment=(
                "选路上游的原因：affinity(会话亲和命中，缓存优先) | weighted(优先级分组+组内加权随机，"
                "常规负载均衡) | failover_affinity(上游失败后换端，命中亲和) | failover_weighted"
                "(上游失败后换端，按优先级+权重重选)；NULL=历史行或未触达选路"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("request_log", "route_reason")
