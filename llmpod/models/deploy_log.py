"""DeployLog ORM 模型（部署全流程日志，append-only）。

一条部署从提交到 running/failed/stopped/deleted 的全过程事件流，统一落此表：

- **管理端编排**（submit / phase 切换 / 失败）：``node_id`` 为空。
- **每节点实例生命周期**（STARTING/RUNNING/READY/FAILED/STOPPED，由
  ``report_instance_status`` 投影）：``node_id`` 有值。

一张表即「全流程」，按 ``deployment_id + node_id + step + level`` 过滤，供前端时间线
可视化与排障：用户可直观看到「哪台机 - 什么步骤 - 什么级别 - 报了什么」。

与 ``task_event`` 的区别：``task_event`` 是任务级状态审计（按 task，驱动 task/node_model
推进）；``deploy_log`` 是部署级全流程日志（按 deployment，含管理端编排事件 + 每节点实例
生命周期），不驱动任何状态机，纯观测。

append-only（只有 ``created_at``，无 ``updated_at``）。``deployment_id`` 不建 DB 外键——
级联删除由应用层 ``deploy_log_service.delete_for_deployment`` 在
``delete_deployment_cascade`` 内显式控制，与 ``task_event``/``task`` 一致（本仓库统一用
逻辑 FK、无 DB FK），非遗漏。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base


class DeployLog(Base):
    """部署全流程日志（append-only；管理端编排 + 每节点实例生命周期统一落此表）。"""

    __tablename__ = "deploy_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="日志ID（自增）")
    deployment_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="部署ID（逻辑 FK → model_deployment）"
    )
    node_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="节点ID（编排事件为空；实例生命周期事件有值，逻辑 FK → node）"
    )
    task_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="任务ID（逻辑 FK → task；编排事件可空）"
    )
    step: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="步骤：queued/staging_model/pull_image/start_head/head_health_check/start_workers/worker_health_check/finalize/running/submit/start/stop/stopping/start_instance/orchestration",
    )
    level: Mapped[str] = mapped_column(
        String(8), nullable=False, default="info", server_default="info", comment="级别：debug | info | warn | error"
    )
    status: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="步骤进度状态快照：pending | running | success | failed | cancelled（非部署态）",
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="可读日志内容")
    extra: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="结构化扩展（trigger/container_id/instance_id/endpoint/exit_code/phase 等）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="记录时间",
    )

    __table_args__ = (
        CheckConstraint("level IN ('debug', 'info', 'warn', 'error')", name="ck_deploy_log_level"),
        CheckConstraint(
            "status IS NULL OR status IN ('pending', 'running', 'success', 'failed', 'cancelled')",
            name="ck_deploy_log_status",
        ),
        Index("ix_deploy_log_deployment_created", "deployment_id", "created_at"),
        Index("ix_deploy_log_deployment_node_created", "deployment_id", "node_id", "created_at"),
        Index("ix_deploy_log_node", "node_id"),
        # 按 created_at 倒序取近窗 top-N（大屏事件流）的走索引路径
        Index("ix_deploy_log_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<DeployLog deployment={self.deployment_id} step={self.step!r} level={self.level!r}>"
