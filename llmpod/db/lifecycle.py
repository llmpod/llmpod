"""统一数据库生命周期管理：init_all_dbs / close_all_dbs。"""

from __future__ import annotations

import logging

from llmpod.config.settings import Settings
from llmpod.db.pg import init_pg, close_pg
from llmpod.db.redis import init_redis, close_redis

log = logging.getLogger("llmpod.db")


async def init_all_dbs(settings: Settings):
    """在启动时初始化所有数据库（管理端：PG+Redis；API 网关进程亦可复用，Redis 用于限流/缓存）。"""
    await init_required_dbs(settings)


async def init_required_dbs(settings: Settings):
    """初始化必需的数据库：PG + Redis。"""
    await init_pg(settings)
    await init_redis(settings)


async def close_all_dbs():
    """在关闭时释放所有数据库连接。"""
    await close_pg()
    await close_redis()
