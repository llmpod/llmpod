"""ProxyServer 鉴权（AuthN）集成测试：Redis 缓存 miss 回源回填 + 状态/过期判定。

覆盖请求管道第 1 级与缓存协议的网关读侧。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from _fakes import FakeSession
from test_api_key_service import FakeRedis

from llmpod.apps.proxyserver.authn import authenticate
from llmpod.config.constants import APIKEY_CACHE_PREFIX
from llmpod.services.api_key_service import KNOWN_APIKEY_PREFIX, warm_apikey_cache
from llmpod.models import ApiKey


def _key(id=1, key_hash=None, status="active", expires_at=None) -> ApiKey:
    return ApiKey(
        id=id,
        key_hash=key_hash or sha256(b"sk-live").hexdigest(),
        tenant_id=None,
        name="k",
        allowed_models=["m1"],
        rpm_limit=60,
        tpm_limit=None,
        total_token_quota=None,
        model_token_quotas={},
        status=status,
        expires_at=expires_at,
    )


class TestAuthenticate:
    async def test_valid_key_survives_shared_ip_miss_flood_after_cache_expiry(self, monkeypatch):
        db = FakeSession()
        db.preload(_key())
        redis = FakeRedis()
        await warm_apikey_cache(db, redis)
        digest = sha256(b"sk-live").hexdigest()
        assert f"{KNOWN_APIKEY_PREFIX}{digest}" in redis.store
        del redis.store[f"{APIKEY_CACHE_PREFIX}{digest}"]  # 300s 快照过期
        # 消融：旧实现只按出口 IP 计数，其他人打满 60 次后真实 Key 回源也被拒。
        import llmpod.apps.proxyserver.authn as authn

        monkeypatch.setattr(authn.time, "time", lambda: 0)
        redis.store["llmpod:apikey-miss:203.0.113.7:0"] = "60"
        assert (await authenticate(db, redis, "sk-live", client_ip="203.0.113.7")).key_id == 1

    async def test_unique_unknown_keys_have_bounded_db_misses_per_ip(self):
        db = FakeSession()
        calls = 0
        original = db.execute

        async def counting(stmt):
            nonlocal calls
            calls += 1
            return await original(stmt)

        db.execute = counting

        class Redis(FakeRedis):
            async def incr(self, key):
                value = int(self.store.get(key, 0)) + 1
                self.store[key] = str(value)
                return value

            async def expire(self, key, seconds):
                return True

        redis = Redis()
        for i in range(80):
            assert await authenticate(db, redis, f"sk-random-{i}", client_ip="203.0.113.7") is None
        assert calls == 60

    async def test_cache_hit_no_pg_query(self):
        """缓存命中直接返回，不查 PG（FakeSession 无预载即证明零回源）。"""
        redis = FakeRedis()
        h = sha256(b"sk-live").hexdigest()
        redis.store[f"{APIKEY_CACHE_PREFIX}{h}"] = json.dumps(
            {
                "key_id": 1,
                "tenant_id": 3,
                "name": "k",
                "allowed_models": ["m1"],
                "rpm_limit": 60,
                "tpm_limit": None,
                "total_token_quota": None,
                "model_token_quotas": {},
                "status": "active",
                "expires_at": None,
            }
        )
        db = FakeSession()  # 空——若查库会 miss 返回 None

        ctx = await authenticate(db, redis, "sk-live")

        assert ctx is not None
        assert ctx.key_id == 1 and ctx.tenant_id == 3
        assert ctx.allowed_models == frozenset({"m1"})
        assert ctx.rpm_limit == 60

    async def test_cache_miss_falls_back_to_pg_and_backfills(self):
        db = FakeSession()
        db.preload(_key(id=9))
        redis = FakeRedis()
        h = sha256(b"sk-live").hexdigest()

        ctx = await authenticate(db, redis, "sk-live")

        assert ctx is not None and ctx.key_id == 9
        # 回填：网关 miss 后写缓存（含 TTL 由 redis 客户端 ex 承担，FakeRedis 记录键即可）
        assert f"{APIKEY_CACHE_PREFIX}{h}" in redis.store
        assert json.loads(redis.store[f"{APIKEY_CACHE_PREFIX}{h}"])["key_id"] == 9

    async def test_disabled_rejected_even_in_cache(self):
        redis = FakeRedis()
        h = sha256(b"sk-live").hexdigest()
        redis.store[f"{APIKEY_CACHE_PREFIX}{h}"] = json.dumps(
            {
                "key_id": 1,
                "tenant_id": None,
                "name": "k",
                "allowed_models": [],
                "rpm_limit": None,
                "tpm_limit": None,
                "total_token_quota": None,
                "model_token_quotas": {},
                "status": "disabled",
                "expires_at": None,
            }
        )

        assert await authenticate(FakeSession(), redis, "sk-live") is None

    async def test_expired_key_rejected(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        db = FakeSession()
        db.preload(_key(id=5, expires_at=past))
        redis = FakeRedis()

        assert await authenticate(db, redis, "sk-live") is None

    async def test_unknown_key_returns_none_with_negative_cache(self):
        """未知 Key：写 60s 负缓存（空串哨兵），二次尝试不再回源 PG。"""
        db = FakeSession()
        redis = FakeRedis()
        calls = {"n": 0}
        orig_execute = db.execute

        async def _counting_execute(stmt):
            calls["n"] += 1
            return await orig_execute(stmt)

        db.execute = _counting_execute

        assert await authenticate(db, redis, "sk-ghost") is None
        ghost_key = f"{APIKEY_CACHE_PREFIX}{sha256(b'sk-ghost').hexdigest()}"
        assert redis.store.get(ghost_key) == ""  # 负缓存哨兵
        # 二次：命中负缓存，不再打 PG
        assert await authenticate(db, redis, "sk-ghost") is None
        assert calls["n"] == 1

    async def test_cache_hit_with_expired_timestamp_rejected(self):
        """缓存里的过期时间也要判（预热后到期未失效窗口内）。"""
        redis = FakeRedis()
        h = sha256(b"sk-live").hexdigest()
        redis.store[f"{APIKEY_CACHE_PREFIX}{h}"] = json.dumps(
            {
                "key_id": 1,
                "tenant_id": None,
                "name": "k",
                "allowed_models": [],
                "rpm_limit": None,
                "tpm_limit": None,
                "total_token_quota": None,
                "model_token_quotas": {},
                "status": "active",
                "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            }
        )

        assert await authenticate(FakeSession(), redis, "sk-live") is None

    async def test_redis_error_degrades_to_pg_not_500(self, monkeypatch):
        """Redis 抖动（异常上抛、熔断尚未 open）：跳缓存直查 PG，鉴权不 fail-open。"""
        import llmpod.apps.proxyserver.redis_guard as redis_guard

        redis_guard.reset_breaker()
        monkeypatch.setattr(redis_guard, "guarded_redis_get", _RaisingGet(), raising=True)
        db = FakeSession()
        db.preload(_key())
        ctx = await authenticate(db, FakeRedis(), "sk-live")
        assert ctx is not None and ctx.key_id == 1
        redis_guard.reset_breaker()


class _RaisingGet:
    """把 guarded_redis_get 替换成「始终抛错」的可调用对象（模拟 Redis 抖动窗口）。"""

    async def __call__(self, redis, cache_key):
        raise ConnectionError("redis unreachable")
