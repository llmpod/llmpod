"""Node 本地任务 ORM 模型（SQLite，独立 metadata）。

与 manager 的 PG 表（``llmpod/models/``）解耦——Node 是轻量进程，本地持久化走 SQLite
（``aiosqlite`` 驱动，经 SQLAlchemy async ORM）。用独立 ``NodeBase`` metadata，**不**注册到
manager 的 PG ``Base``，避免 ``Base.metadata.create_all`` 把本地表建进 PG。

``LocalTask`` 既是 ORM 映射类，也作 agent/executor 的内存载体（detached 实例——store 用
``expire_on_commit=False`` 的短会话，脱离 session 后属性仍可读）。

**payload 不含 secrets**——secrets 仅内存随 ``ensure_task`` 传入，永不落盘/落日志。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column

from llmpod.tunnel.schemas import TaskPayload, TaskState


class NodeBase(MappedAsDataclass, DeclarativeBase):
    """Node 本地 ORM 基类（独立 metadata，不混入 manager 的 PG ``Base``）。"""

    pass


class LocalTask(NodeBase):
    """``local_tasks`` 行：manager ``task`` 表的 Node 本地镜像。``payload`` 不含 secrets。

    用 ``MappedAsDataclass``：兼具 ORM 映射与 dataclass 构造语义（带默认值的 ``__init__``），
    便于 agent/executor 直接 ``LocalTask(task_id=..., status=...)`` 构造后 ``store.upsert``。

    ``status`` / ``progress`` / ``last_error`` 为执行期可变列（executor 推进）；其余为任务
    配方快照（``ensure_task`` 写入，幂等 upsert 不覆盖执行期进度——见 store ``upsert``）。
    """

    __tablename__ = "local_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_key: Mapped[str] = mapped_column(String(128))
    node_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    timeout_sec: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    payload: Mapped[dict] = mapped_column(JSON, default_factory=dict)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        server_default=func.now(),
        onupdate=func.now(),
        default=None,
    )

    @classmethod
    def from_payload(cls, tp: TaskPayload) -> "LocalTask":
        """``TaskPayload``（wire）→ ``LocalTask``（本地行）。"""
        return cls(
            task_id=tp.id,
            type=tp.type,
            resource_type=tp.resource_type,
            resource_key=tp.resource_key,
            node_id=tp.node_id,
            status=tp.status,
            attempt=tp.attempt,
            progress=tp.progress,
            timeout_sec=tp.timeout_sec,
            payload=tp.payload or {},
        )

    def to_state(self) -> TaskState:
        """``LocalTask`` → ``TaskState``（``sync_node_state`` 上报）。"""
        return TaskState(
            id=self.task_id,
            status=self.status,
            progress=self.progress,
            resource_key=self.resource_key,
            last_error=self.last_error,
        )
