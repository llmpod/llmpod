"""九处列注释补落：0012 后只改了 ORM comment、未配 alter_column 的欠账。

Revision ID: 0013_comment_backfill
Revises: 0012_usage_estimated
Create Date: 2026-10-21

存量库 alembic upgrade 不回放 ORM 层的 comment 改动（基线不回放 DDL，
增量链只认 revision 里显式写的语句）。本 revision 把如下九列的注释
按当前 ORM 现值补齐 alter_column：

- api_key.tenant_id / api_key.budget_frozen（平台直发 Key 语义 + 预算冻结口径）
- audit_log.tenant_id（归属列 NULL/0 分工口径）
- cluster.type
- model_deployment.phase / model_deployment.health_status
- model_deployment_instance.status（投影状态口径）
- route.failover_enabled / route.failover_max_retries（术语批改「选中→选定」）

另带 request_log.tenant_id 的口径句改写（「0=平台租户」在收敛后是不可达值，
改为「平台视角=不传或传 0 取 NULL ∪ 0」的查询口径描述）与
balance_hold.expires_at 注释更新（reaper 已消费该列，「预留不消费」说法过时）。

纯注释变更，无结构影响；空库路径（create_all 一次成型）天然一致。
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_comment_backfill"
down_revision: Union[str, None] = "0012_usage_estimated"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "api_key", "tenant_id", existing_type=None, comment="所属租户ID（逻辑外键 → tenant.id；空=平台直发）"
    )
    op.alter_column(
        "api_key",
        "budget_frozen",
        existing_type=None,
        comment="Key 在途预算冻结（元）：预扣时 +、结算/解冻时 -；校验 budget_spent + budget_frozen <= budget_limit",
    )
    op.alter_column(
        "audit_log",
        "tenant_id",
        existing_type=None,
        comment="数据归属租户ID（逻辑外键 → tenant.id；空=无租户维度的平台级操作，0=平台租户自身操作）",
    )
    op.alter_column("cluster", "type", existing_type=None, comment="集群类型：docker")
    op.alter_column(
        "model_deployment",
        "phase",
        existing_type=None,
        comment="内部编排子态（仅 status=creating 期推进；scale 时 running 期暂驻 SCALING）：PENDING | STAGING_MODEL | PULL_IMAGE | START_HEAD | WAIT_HEAD_READY | START_WORKERS | WAIT_WORKERS_READY | FINALIZING | RUNNING | SCALING | STOPPING（多 worker 阶段冻结未实现）",
    )
    op.alter_column(
        "model_deployment",
        "health_status",
        existing_type=None,
        comment="运行时健康（推论）：healthy | degraded（实例异常/自动恢复中）；仅 running 期维护，creating 期为 NULL",
    )
    op.alter_column(
        "model_deployment_instance",
        "status",
        existing_type=None,
        comment="投影状态：provisioning | starting | ready | down（全部分片 READY + head 健康 ⇔ ready）",
    )
    op.alter_column(
        "route",
        "failover_enabled",
        existing_type=None,
        comment="选定端点失败后是否切换到同协议其他成员（默认开；单成员路由退化行为与关闭一致）",
    )
    op.alter_column(
        "route",
        "failover_max_retries",
        existing_type=None,
        comment="failover 切换次数上限（默认 2，共 3 次尝试；0=不切换）",
    )
    op.alter_column(
        "request_log",
        "tenant_id",
        existing_type=None,
        comment="租户ID（逻辑外键 → tenant.id；NULL=平台直发 Key 的请求；平台视角=不传或传 0 取 NULL ∪ 0，传 N≥1 精确等值）",
    )
    op.alter_column(
        "balance_hold",
        "expires_at",
        existing_type=None,
        comment="冻结租约到期时间：hold_reaper 据此回收超期未清的悬空冻结",
    )


def downgrade() -> None:
    # 纯注释补落，无可逆语义：downgrade 为 no-op
    pass
