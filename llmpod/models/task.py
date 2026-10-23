"""Task ORM 模型（domain-neutral 统一任务底座：Manager→Node 的声明式任务）。

归属语义唯一来自 ``resource_type + resource_key + node_id``——**无属主列**。
deployment↔task 关系经 Instance 表 join 回答（见 ``PlacementService.recompute_demand``，
本就不读任何 task 属主列）。``payload`` **不含 secrets**（secrets 随 RPC 内存下发，
不入库/不落日志）。

任务类型经 ``type`` 受控词表扩展（当前含 ENSURE_MODEL / PULL_IMAGE / START_INSTANCE /
STOP_INSTANCE 四类），表结构按通用任务底座设计。

去重：``uq_task_active_resource`` partial unique index 保证同一 resource_key 同时只有
一个 active(PENDING/RUNNING) task；``uq_task_resource_key_attempt``。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, TimestampMixin


class Task(Base, TimestampMixin):
    """统一任务（domain-neutral；String PK；resource_key 去重 + attempt 重试；无属主列）。"""

    __tablename__ = "task"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, autoincrement=False, comment="任务ID（UUID，manager 分配）"
    )
    resource_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="资源类型：MODEL_PLACEMENT | ...（受控词表，registry 兜底，无 DB CHECK）"
    )
    resource_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="资源键，如 model:{model_id}:{node_id}",
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="目标节点ID（→ node.id）")
    type: Mapped[str] = mapped_column(String(32), nullable=False, comment="任务类型：ENSURE_MODEL | ...")
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="PENDING",
        server_default="PENDING",
        comment="状态：PENDING | RUNNING | SUCCESS | FAILED | CANCELLED",
    )
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="任务参数（不含 secrets）",
    )
    timeout_sec: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="超时秒数")
    attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="尝试序号（同 resource_key 递增；Node 本地重试不增 attempt）",
    )
    progress: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="进度百分比 0-100",
    )
    stage: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="当前子阶段快照（按任务类型词表，如 downloading/pulling；终态后冻结）",
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最近失败原因")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="开始执行时间")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="结束时间")

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED')",
            name="ck_task_status",
        ),
        Index("ix_task_node", "node_id"),
        Index("ix_task_resource_key", "resource_key"),
        Index("uq_task_resource_key_attempt", "resource_key", "attempt", unique=True),
        # DB 层去重兜底：同一 resource_key 同时只允许一个 active task
        Index(
            "uq_task_active_resource",
            "resource_key",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Task id={self.id[:8]} type={self.type!r} status={self.status!r}>"
