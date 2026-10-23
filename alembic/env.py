"""Alembic 迁移环境：连接串与元数据都来自 llmpod 主应用配置。

- engine 用 llmpod Settings 的 PG URL（LLMPOD_PG_* 环境变量），
  与运行时同一配置来源，不在这里重复维护连接信息；
- target_metadata 挂 Base.metadata 并导入 llmpod.models 注册全部模型，
  供 ``alembic revision --autogenerate`` 对比生成迁移；
- offline/online 两种模式都支持（本仓库只用 online；offline 留标准能力）。
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from llmpod.config.settings import settings as llmpod_settings
from llmpod.db.base import Base
import llmpod.models  # noqa: F401 - 导入以注册所有模型到 Base.metadata

config = context.config

# 让 alembic 日志走自己的配置（alembic.ini [logger_alembic]）
if config.config_file_name is not None:
    from logging.config import fileConfig

    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：不连库，只生成 SQL 脚本（本仓库暂不使用，保留标准能力）。"""
    context.configure(
        url=llmpod_settings.pg_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：异步引擎起连接后在同步上下文里跑迁移。"""
    from sqlalchemy.ext.asyncio import async_engine_from_config

    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = llmpod_settings.pg_url
    connectable = async_engine_from_config(section, prefix="sqlalchemy.")

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
