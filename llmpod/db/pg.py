"""PostgreSQL 异步连接管理。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from llmpod.config.settings import Settings
from llmpod.core.exceptions import ConfigError

log = logging.getLogger("llmpod.db.pg")

# 全局单例
async_engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None


async def init_pg(settings: Settings):
    """初始化 PostgreSQL 异步引擎和会话工厂。

    只建连接不做 DDL：schema 由 scripts/init_db.py 的 sync_schema 负责
    （空库 create_all + 打版本戳，存量库走 Alembic 迁移链），管理端、
    API 网关、节点各角色统一复用本函数，不再随启动各自 create_all。
    """
    global async_engine, AsyncSessionLocal

    async_engine = create_async_engine(
        settings.pg_url,
        pool_size=settings.pg_pool_size,
        max_overflow=settings.pg_max_overflow,
        pool_pre_ping=True,
        echo=False,
    )
    AsyncSessionLocal = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # 测试连接
    async with async_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    log.info("PostgreSQL connected: %s:%s/%s", settings.pg_host, settings.pg_port, settings.pg_database)


async def close_pg():
    """释放 PostgreSQL 引擎并关闭所有连接。"""
    global async_engine, AsyncSessionLocal
    if async_engine:
        await async_engine.dispose()
        async_engine = None
        AsyncSessionLocal = None
        log.info("PostgreSQL connections closed")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖项：每个请求产出一个 AsyncSession。"""
    if AsyncSessionLocal is None:
        raise ConfigError("PostgreSQL 未初始化——请先调用 init_pg()")
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """非请求上下文：数据库会话的异步上下文管理器。"""
    if AsyncSessionLocal is None:
        raise ConfigError("PostgreSQL 未初始化——请先调用 init_pg()")
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_engine() -> AsyncEngine:
    """获取全局异步引擎。"""
    if async_engine is None:
        raise ConfigError("PostgreSQL 未初始化——请先调用 init_pg()")
    return async_engine
