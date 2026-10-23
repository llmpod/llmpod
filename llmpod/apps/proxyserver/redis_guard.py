"""API 网关 Redis 熔断降级：热路径对 Redis 故障的统一处置。

背景：鉴权缓存读（authn.redis.get）与限流计数（limits.incr/incrby）裸调 Redis，
Redis 抖动/宕机期间全部推理请求 500——单点依赖把「缓存故障」放大成「网关不可用」。

处置口径（两路径不同，安全面优先）：
- **鉴权不 fail-open**：breaker open 时跳过缓存读写，直接走 PG 点查（既有 miss
  回源路径）。代价是降级窗口内每请求一次 PG 点查——Redis 故障本就是异常态，
  可接受；负缓存/miss-limiter 同样跳过（它们只是放大防护，非正确性依赖）。
- **限流允许 fail-open**（``redis_fail_mode="open"``，默认）：breaker open 时
  RPM/TPM/配额计数直接放行 + 递增降级指标 + 每 open 周期首条 warning。钱包余额
  与 Key 预算在 PG，资金闸不受影响；用量记录照落 request_log，Redis 恢复后
  ``rebuild_quota_counters`` 可重建（与启动重建同路径）。
  ``redis_fail_mode="closed"`` 维持现状（异常上抛 500），留给运营选择。

breaker：连续 ``redis_breaker_failures`` 次失败 → open；open 持续
``redis_breaker_recovery_s`` 后半开试探（放一个请求过），成功即 closed。进程
内存态、无共享——网关默认单副本（与 scraper 单 manager 同族部署假设）。
"""

from __future__ import annotations

import logging
import time

from llmpod.config.settings import settings

log = logging.getLogger("llmpod.apps.proxyserver.redis_guard")

# 熔断器单例（网关进程内唯一；模块级实例 + reset 钩子供测试）
_breaker = None


class RedisBreaker:
    """连续失败计数熔断：closed（正常）→ open（降级）→ half-open（试探）。"""

    def __init__(self, failure_threshold: int, recovery_seconds: float):
        self._threshold = failure_threshold
        self._recovery_s = recovery_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open = False

    def allow(self) -> bool:
        """当前是否放行 Redis 调用（closed/half-open 放行，open 拒绝）。"""
        if self._opened_at is None:
            return True
        if time.monotonic() - self._opened_at >= self._recovery_s:
            self._half_open = True  # 过恢复期：放一个试探
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        if self._opened_at is not None:
            self._opened_at = None
            self._half_open = False
            log.info("redis breaker closed（恢复）")

    def record_failure(self) -> None:
        if self._opened_at is not None:
            if self._half_open:
                # 试探失败：回到 open，重新计恢复期
                self._half_open = False
                self._opened_at = time.monotonic()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()
            log.warning(
                "redis breaker opened（连续 %d 次失败，%.0fs 后半开试探）：鉴权回源 PG / 限流%s",
                self._failures,
                self._recovery_s,
                "放行" if settings.redis_fail_mode == "open" else "维持硬失败",
            )

    @property
    def open(self) -> bool:
        return self._opened_at is not None and not self._half_open


def get_breaker() -> RedisBreaker:
    """进程内单例（惰性初始化）。"""
    global _breaker
    if _breaker is None:
        _breaker = RedisBreaker(settings.redis_breaker_failures, settings.redis_breaker_recovery_s)
    return _breaker


def reset_breaker() -> None:
    """测试钩子：重置单例。"""
    global _breaker
    _breaker = None


async def guarded_redis_get(redis, key: str):
    """鉴权缓存读：breaker open 返回哨兵 _BYPASS（跳缓存直查 PG）；异常计失败并上抛。

    上抛而非吞掉——authn 对异常的处置（回源 PG）在调用方分支里，这里只负责
    「open 时别去碰 Redis」与「成败计数」。
    """
    breaker = get_breaker()
    if not breaker.allow():
        return _BYPASS
    try:
        out = await redis.get(key)
    except Exception:
        breaker.record_failure()
        raise
    breaker.record_success()
    return out


class _Bypass:
    """open 态的「跳过缓存」哨兵（与 None/缓存值可区分）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance


_BYPASS = _Bypass()
