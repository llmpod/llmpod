"""Instance ORM 模型（部署的一个容器实例，CRD 风格，String PK）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, TimestampMixin


class Instance(Base, TimestampMixin):
    """部署实例（一个 ModelDeployment 展开为多个 Instance，每个 = 一个容器）。

    实例长期状态权威来源：启动/就绪/失败/停止的迁移经 Node ``report_instance_status``
    上报落库（``task_ingest``），容器阶段 reconciler 读它推进部署 phase。
    """

    __tablename__ = "instance"

    instance_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        autoincrement=False,
        comment="实例ID（UUID 字符串主键）",
    )
    # 逻辑外键 → model_deployment.deployment_id / node.id（无 DB 级 FK）
    deployment_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="所属部署ID（→ model_deployment.deployment_id，便于级联）"
    )
    # 逻辑外键 → model_deployment_instance.instance_id（无 DB 级 FK）
    model_deployment_instance_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="所属引擎实例ID（→ model_deployment_instance.instance_id；迁移期可空，四层挂载权威列）",
    )
    role: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="full", comment="角色：full | prefill | decode | ..."
    )
    rank: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="实例全局序号（拓扑展开顺序 0..N-1；head=0，worker>0），task resource_key 用",
    )
    assigned_node: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="分配的节点ID（→ node.id，调度后填）"
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="PENDING",
        comment="长期状态：PENDING | STARTING | RUNNING | READY | CRASHED | FAILED | STOPPING | STOPPED",
    )
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="容器标识（Node 启动后回填）")
    endpoint: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="实例服务端点（Ready 后回填，如 http://ip:port）"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最近一次失败原因")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="容器启动时间")
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="就绪（READY）时间"
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="停止（STOPPED）时间"
    )
    spec: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="实例 spec：tp/nnodes/node_rank/assigned_gpus/master_addr/master_port/...（CRD 扩展槽）",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'STARTING', 'RUNNING', 'READY', 'CRASHED', 'FAILED', 'STOPPING', 'STOPPED')",
            name="ck_instance_status",
        ),
        Index("idx_instance_deployment_id", "deployment_id"),
        Index("idx_instance_status", "status"),
        Index("idx_instance_assigned_node", "assigned_node"),
        Index("idx_instance_engine_instance_id", "model_deployment_instance_id"),
    )

    def __repr__(self) -> str:
        return f"<Instance instance_id={self.instance_id[:8]} status={self.status!r}>"
