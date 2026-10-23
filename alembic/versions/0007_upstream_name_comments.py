"""上游展示名口径切换：request_log.provider_name / tenant_id 与 model_endpoint.name 列注释随语义更新。

Revision ID: 0007_upstream_name_comments
Revises: 0006_request_log_route_reason
Create Date: 2026-09-28

「上游」的展示值从选填的 model_endpoint.provider_name（供应商名）换成恒非空的
model_endpoint.name（端点展示名：本地=部署名、外部=登记名）；计量列表的平台视角
口径定为 NULL ∪ 0（与审计日志同源）。列的 DDL 宽度不变（provider_name 维持
varchar(100)，超长走写入侧截断），但注释描述的语义变了——存量库 alembic upgrade
不会自动带上 comment 改动，单独以 alter_column 落注释，避免库里注释与代码语义相反。
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_upstream_name_comments"
down_revision: Union[str, None] = "0006_request_log_route_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "request_log",
        "provider_name",
        comment=(
            "上游展示名（快照自端点 name：本地=部署名，外部=端点名）；NULL=未触达上游。"
            "列名沿用 provider_name 是历史与前端 JSON 契约所限，语义已不是「供应商」；"
            "varchar(100) 短于端点名上限 255，写入侧按列宽截断兜底（见 apps/proxyserver/request_logger.py）"
        ),
    )
    op.alter_column(
        "request_log",
        "tenant_id",
        comment="租户ID（逻辑外键 → tenant.id；NULL=平台直发 Key 的请求，0=平台租户；admin 平台视角取 NULL ∪ 0）",
    )
    op.alter_column(
        "model_endpoint",
        "name",
        comment=(
            "端点展示名（本地=所属部署名，多副本带 ·副本N 后缀；外部=登记时自定义）。"
            "恒非空，也是「上游」列与上游依赖明细的展示值"
        ),
    )


def downgrade() -> None:
    op.alter_column(
        "model_endpoint",
        "name",
        comment="端点展示名（本地=模型名，外部=登记时自定义）",
    )
    op.alter_column(
        "request_log",
        "tenant_id",
        comment="租户ID（逻辑外键 → tenant.id；空=平台直发 Key 的请求）",
    )
    op.alter_column(
        "request_log",
        "provider_name",
        comment="上游供应商名（快照自端点 provider_name，本地端点可空）",
    )
