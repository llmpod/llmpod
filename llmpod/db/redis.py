"""Redis 异步连接管理。"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from llmpod.config.settings import Settings
from llmpod.core.exceptions import ConfigError

log = logging.getLogger("llmpod.db.redis")

# 全局单例
redis_pool: aioredis.ConnectionPool | None = None
_redis_client: aioredis.Redis | None = None


async def init_redis(settings: Settings):
    """初始化 Redis 异步连接池。"""
    global redis_pool, _redis_client

    redis_pool = aioredis.ConnectionPool(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        db=settings.redis_db,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
    )
    _redis_client = aioredis.Redis(connection_pool=redis_pool)

    # 测试连接
    await _redis_client.ping()
    log.info("Redis connected: %s:%s (db=%d)", settings.redis_host, settings.redis_port, settings.redis_db)


async def close_redis():
    """关闭 Redis 连接池。"""
    global redis_pool, _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
    if redis_pool:
        await redis_pool.aclose()
        redis_pool = None
        log.info("Redis connections closed")


def get_redis() -> aioredis.Redis:
    """获取 Redis 客户端实例。"""
    if _redis_client is None:
        raise ConfigError("Redis 未初始化——请先调用 init_redis()")
    return _redis_client
