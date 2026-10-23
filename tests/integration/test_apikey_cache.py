"""ApiKey Redis 缓存工具测试（管理端与网关的跨端缓存契约）。

- warm_apikey_cache：全量预热——PG 全部 Key 写入 llmpod:apikey:{sha256}，
  value 为配置 JSON（只含哈希不含明文）、TTL 兜底；
- 缓存 JSON schema 是管理端（写/删）与网关（读/回填）的共同契约：
  tenant_id/name/allowed_models/限流/配额/status/expires_at(ISO)。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from _fakes import FakeSession
from test_api_key_service import FakeRedis

from llmpod.config.constants import APIKEY_CACHE_PREFIX
from llmpod.models import ApiKey
from llmpod.services.api_key_service import warm_apikey_cache


def _key(key_hash: str, **ovr) -> ApiKey:
    base = dict(
        key_hash=key_hash,
        tenant_id=None,
        name="k",
        allowed_models=["m1"],
        rpm_limit=60,
        tpm_limit=100000,
        total_token_quota=None,
        model_token_quotas={},
        status="active",
        expires_at=None,
    )
    base.update(ovr)
    return ApiKey(**base)


class FakeRedisWithTTL(FakeRedis):
    def __init__(self):
        super().__init__()
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None, **kw):
        await super().set(key, value)
        if ex is not None:
            self.ttls[key] = ex


class TestWarmApikeyCache:
    async def test_writes_all_keys_with_ttl(self):
        db = FakeSession()
        db.preload(_key("a" * 64, id=1))
        db.preload(_key("b" * 64, id=2, status="disabled"))  # disabled 也预热：网关据此即时拒绝
        redis = FakeRedisWithTTL()

        count = await warm_apikey_cache(db, redis)

        assert count == 2
        for h in ("a" * 64, "b" * 64):
            assert redis.store[f"{APIKEY_CACHE_PREFIX}{h}"]
            assert redis.ttls[f"{APIKEY_CACHE_PREFIX}{h}"] > 0  # TTL 兜底存在

    async def test_cache_value_schema_contract(self):
        db = FakeSession()
        exp = datetime(2030, 1, 1, tzinfo=timezone.utc)
        db.preload(
            _key(
                "c" * 64,
                id=3,
                tenant_id=3,
                name="tk",
                total_token_quota=1000,
                model_token_quotas={"m1": 500},
                expires_at=exp,
            )
        )
        redis = FakeRedisWithTTL()

        await warm_apikey_cache(db, redis)

        raw = redis.store[f"{APIKEY_CACHE_PREFIX}{'c' * 64}"]
        payload = json.loads(raw)
        # 平台契约字段集：网关鉴权所需全部信息，绝不含明文
        assert payload == {
            "key_id": 3,  # request_log 关联 Key 行
            "tenant_id": 3,
            "name": "tk",
            "allowed_models": ["m1"],
            "rpm_limit": 60,
            "tpm_limit": 100000,
            "total_token_quota": 1000,
            "model_token_quotas": {"m1": 500},
            # Decimal 无损往返走 str；网关据此判平台 Key 预扣要不要进 DB 冻结
            "budget_limit": None,
            "status": "active",
            "expires_at": exp.isoformat(),
        }
        assert "sk-" not in raw  # 不含明文

    async def test_empty_db_writes_nothing(self):
        redis = FakeRedisWithTTL()
        assert await warm_apikey_cache(FakeSession(), redis) == 0
        assert redis.store == {}


class TestCacheKeyFormat:
    def test_prefix_is_platform_contract(self):
        """key 前缀是跨面契约，不得变动。"""
        assert APIKEY_CACHE_PREFIX == "llmpod:apikey:"
