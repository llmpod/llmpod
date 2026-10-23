"""Node 本地任务持久化（SQLAlchemy async ORM + aiosqlite 驱动）。

重启后读表恢复任务上下文。
``LocalTask`` ORM 模型见 :mod:`llmpod.apps.node.models`。**payload 不含 secrets**。

并发：每个方法用独立短会话（``async with session`` → commit 即归还连接池），无需手锁；
SQLite 由 SQLAlchemy 连接池 + aiosqlite 串行化写入。
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from llmpod.apps.node.models import LocalTask, NodeBase
from llmpod.config.constants import TASK_INCOMPLETE_STATUSES

log = logging.getLogger("llmpod.apps.node.agent_store")

# 执行中状态——upsert 默认不覆盖既有执行期进度（避免 ensure_task 重发冲掉 node 本地进度）
_ACTIVE = TASK_INCOMPLETE_STATUSES


class TaskStore:
    """SQLAlchemy async ORM 本地任务表（SQLite + aiosqlite）。"""

    def __init__(self, db_path: str):
        self._path = db_path
        self._engine = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    async def init(self) -> None:
        """打开/创建库 + 建表。父目录自动创建。"""
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._path}", echo=False)
        self._sessionmaker = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(NodeBase.metadata.create_all)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None

    def _session(self) -> AsyncSession:
        if self._sessionmaker is None:
            raise RuntimeError("TaskStore 未 init()，无法获取会话")
        return self._sessionmaker()

    async def upsert(self, t: LocalTask, *, overwrite_running: bool = False) -> None:
        """写入/更新任务行。

        默认 ``overwrite_running=False``：若行已存在且处于 active，**不覆盖** 执行期进度
        （``status`` / ``progress`` / ``last_error``）——manager 重发同一任务不应冲掉 node
        本地已推进的进度（契约 ``ensure_task`` 幂等语义）。配方列（type/resource_key/...）
        仍刷新（同任务配方不变，幂等）。``overwrite_running=True`` / 终态行：全列覆盖（允许重新激活）。
        """
        async with self._session() as s:
            existing = await s.get(LocalTask, t.task_id)
            if existing is None:
                s.add(t)
            else:
                # 配方列两分支同刷（重新下发即同配方，幂等）；差异在执行期列——
                # 活跃行不覆盖进度，终态行 / overwrite_running 全列覆盖（允许重新激活）
                existing.type = t.type
                existing.resource_type = t.resource_type
                existing.resource_key = t.resource_key
                existing.node_id = t.node_id
                existing.attempt = t.attempt
                existing.timeout_sec = t.timeout_sec
                existing.payload = t.payload
                # 活跃行不覆盖执行期列（progress 继续跑）；终态行 / overwrite_running
                # 全列覆盖（允许重新激活）
                if existing.status not in _ACTIVE or overwrite_running:
                    existing.status = t.status
                    existing.progress = t.progress
                    existing.last_error = t.last_error
            await s.commit()

    async def get(self, task_id: str) -> LocalTask | None:
        async with self._session() as s:
            return await s.get(LocalTask, task_id)

    async def list_all(self) -> list[LocalTask]:
        async with self._session() as s:
            return list((await s.execute(select(LocalTask))).scalars())

    async def list_active(self) -> list[LocalTask]:
        async with self._session() as s:
            stmt = select(LocalTask).where(LocalTask.status.in_(_ACTIVE))
            return list((await s.execute(stmt)).scalars())

    async def update_status(
        self,
        task_id: str,
        status: str,
        *,
        progress: int | None = None,
        last_error: str | None = None,
    ) -> None:
        """推进执行期状态。``progress`` / ``last_error`` 传 None 表示不改（对应原 COALESCE 语义）。"""
        async with self._session() as s:
            t = await s.get(LocalTask, task_id)
            if t is None:
                return
            t.status = status
            if progress is not None:
                t.progress = progress
            if last_error is not None:
                t.last_error = last_error
            await s.commit()

    async def delete(self, task_id: str) -> None:
        async with self._session() as s:
            t = await s.get(LocalTask, task_id)
            if t is not None:
                await s.delete(t)
            await s.commit()
