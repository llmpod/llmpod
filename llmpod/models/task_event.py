"""TaskEvent ORM 模型（任务状态变更审计日志，Node→Manager 上报）。

append-only 事件流（只有 created_at，无 updated_at）。``extra`` 不含 secrets，
``message`` 必须 scrub。用于排障与审计。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base


class TaskEvent(Base):
    """任务事件（排障/审计；extra 不含 secrets，message 必须 scrub）。"""

    __tablename__ = "task_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="事件ID（自增）")
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="任务ID（→ task.id）")
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="上报节点ID（→ node.id）")
    status: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="任务状态快照")
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="进度百分比")
    extra: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="扩展字段（stage/bytes_done/bytes_total/local_dir/model_path；不含 secrets）",
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="可读说明（必须 scrub secrets）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="上报时间",
    )

    __table_args__ = (Index("ix_task_event_task_id", "task_id"),)

    def __repr__(self) -> str:
        return f"<TaskEvent task_id={self.task_id} status={self.status!r}>"
