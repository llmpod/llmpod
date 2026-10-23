"""Redis 熔断降级单测：breaker 状态机 + authn 回源 + 限流 fail-open。"""

from __future__ import annotations

import pytest

from llmpod.apps.proxyserver import redis_guard
from llmpod.apps.proxyserver.limits import RateLimitError, RateLimiter
from llmpod.apps.proxyserver.redis_guard import RedisBreaker, reset_breaker


@pytest.fixture(autouse=True)
def _fresh_breaker():
    reset_breaker()
    yield
    reset_breaker()


class _DeadRedis:
    async def get(self, key):
        raise ConnectionError("redis down")

    async def incr(self, key):
        raise ConnectionError("redis down")

    async def incrby(self, key, n):
        raise ConnectionError("redis down")


class TestBreaker:
    def test_opens_after_threshold(self):
        b = RedisBreaker(failure_threshold=3, recovery_seconds=30)
        assert b.allow()
        b.record_failure()
        b.record_failure()
        assert b.allow()  # 未到阈值
        b.record_failure()
        assert not b.allow()  # open

    def test_recovery_half_open_then_close(self, monkeypatch):
        b = RedisBreaker(failure_threshold=1, recovery_seconds=0.01)
        b.record_failure()
        assert not b.allow()
        import time

        time.sleep(0.02)
        assert b.allow()  # half-open 放试探
        b.record_success()
        assert b.allow() and not b.open  # closed

    def test_half_open_failure_reopens(self, monkeypatch):
        import time

        b = RedisBreaker(failure_threshold=1, recovery_seconds=0.01)
        b.record_failure()
        time.sleep(0.02)
        assert b.allow()
        b.record_failure()  # 试探失败
        assert not b.allow()  # 回 open


async def test_authn_open_bypasses_cache_direct_pg(monkeypatch):
    """breaker open → guarded_get 返回 _BYPASS（调用方跳缓存直查 PG）。"""
    b = RedisBreaker(failure_threshold=1, recovery_seconds=30)
    b.record_failure()  # 直接置 open
    monkeypatch.setattr(redis_guard, "get_breaker", lambda: b)
    out = await redis_guard.guarded_redis_get(_DeadRedis(), "k")
    assert out is redis_guard._BYPASS


async def test_authn_failure_counts_and_raises(monkeypatch):
    """Redis 故障 → 计失败并上抛（authn 调用方回源 PG）。"""
    b = RedisBreaker(failure_threshold=5, recovery_seconds=30)
    monkeypatch.setattr(redis_guard, "get_breaker", lambda: b)
    with pytest.raises(ConnectionError):
        await redis_guard.guarded_redis_get(_DeadRedis(), "k")
    with pytest.raises(ConnectionError):
        await redis_guard.guarded_redis_get(_DeadRedis(), "k")


async def test_rate_limiter_fail_open_when_open(monkeypatch):
    """breaker open → check 直接放行（不触 Redis）。"""
    b = RedisBreaker(failure_threshold=1, recovery_seconds=30)
    b.record_failure()
    monkeypatch.setattr(redis_guard, "get_breaker", lambda: b)
    limiter = RateLimiter(_DeadRedis())
    await limiter.check(key_id=1, rpm_limit=10, tpm_limit=100, est_tokens=5)  # 不抛即放行


async def test_rate_limiter_fail_open_on_error_default(monkeypatch):
    """Redis 抛错 + 默认 fail_mode=open → 放行（不再 500），且异常计入熔断。"""
    b = RedisBreaker(failure_threshold=2, recovery_seconds=30)
    monkeypatch.setattr(redis_guard, "get_breaker", lambda: b)
    limiter = RateLimiter(_DeadRedis())
    await limiter.check(key_id=1, rpm_limit=10, tpm_limit=100, est_tokens=5)
    b.record_failure()  # 阈值 2：上面那次 + 这次 → open（证明失败确实被计数）
    assert not b.allow()


async def test_rate_limiter_closed_mode_keeps_failing(monkeypatch):
    """redis_fail_mode=closed → Redis 异常维持上抛（运营显式选择硬失败）。"""
    from llmpod.config.settings import settings

    b = RedisBreaker(failure_threshold=99, recovery_seconds=30)
    monkeypatch.setattr(redis_guard, "get_breaker", lambda: b)
    monkeypatch.setattr(settings, "redis_fail_mode", "closed")
    limiter = RateLimiter(_DeadRedis())
    with pytest.raises(ConnectionError):
        await limiter.check(key_id=1, rpm_limit=10, tpm_limit=100, est_tokens=5)


async def test_rate_limit_rejection_not_counted_as_failure(monkeypatch):
    """业务拒绝（RateLimitError）不是 Redis 故障，不进熔断计数。"""

    class _LimitedRedis:
        async def incrby(self, key, n):
            return 999  # 已超限

        async def incr(self, key):
            return 1

        async def expire(self, key, ttl):
            return True

        async def get(self, key):
            return None

    b = RedisBreaker(failure_threshold=1, recovery_seconds=30)
    monkeypatch.setattr(redis_guard, "get_breaker", lambda: b)
    limiter = RateLimiter(_LimitedRedis())
    with pytest.raises(RateLimitError):
        await limiter.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=5)
    assert b.allow()  # 未 open
