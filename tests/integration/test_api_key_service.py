"""api_key_service 集成测试（FakeSession + 真实 service 逻辑）。

覆盖写入侧约束与失效协议：
- 签发：明文 sk- 前缀返回；DB 落 SHA-256（鉴权）+ 可逆加密密文（回显）；
- allowed_models ⊆ 租户可用集（published − active 封禁）由管理端写入时强制；
  平台 Key（tenant_id=None）跳过租户封禁校验；
- 未知路由拒绝；禁用 = status 翻转 + 管理端 DEL 缓存 key（平台契约）。
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from _fakes import CLEARABLE_KEY_FIELDS, FakeSession

from llmpod.config.constants import APIKEY_CACHE_PREFIX
from llmpod.core.exceptions import LLMPodValidationError, NotFound
from llmpod.models import ApiKey, RequestLog, Route, TenantModelBlock
from llmpod.services.api_key_service import (
    UNSET,
    create_api_key,
    delete_api_key,
    disable_api_key,
    enable_api_key,
    reset_budget,
    update_api_key,
)


class _FakePipe:
    """非事务 pipeline：缓冲 set/delete，execute 一次刷。"""

    def __init__(self, r: "FakeRedis"):
        self._r = r
        self._ops: list[tuple] = []

    def set(self, key, value, ex=None):
        self._ops.append(("set", key, value, ex))
        return self

    def delete(self, *keys):
        self._ops.append(("delete", keys))
        return self

    async def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "set":
                await self._r.set(op[1], op[2], ex=op[3])
                out.append(True)
            else:
                out.append(await self._r.delete(*op[1]))
        self._ops = []
        return out


class FakeRedis:
    def __init__(self):
        self.deleted: list[str] = []
        self.store: dict[str, str] = {}

    def pipeline(self, transaction: bool = True):
        return _FakePipe(self)

    async def delete(self, *keys):
        for k in keys:
            self.deleted.append(k)
            self.store.pop(k, None)
        return len(keys)

    async def set(self, key, value, **kw):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def scan_iter(self, match=None):
        """异步生成器形态（与 redis.asyncio 一致：async for 直接迭代）。"""
        import fnmatch

        for k in list(self.store):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


def _route(name: str, route_id: int = 7) -> Route:
    return Route(id=route_id, name=name, status="published")


def _ledger_row(id: int, key_id: int, model: str, total: int) -> RequestLog:
    from datetime import datetime, timezone

    return RequestLog(
        id=id,
        request_id=f"r{id}",
        tenant_id=None,
        api_key_id=key_id,
        model_alias=model,
        operation="chat.completions",
        protocol="openai",
        upstream_type="local",
        retry_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=total,
        status="success",
        http_status=200,
        snapshot_disabled=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _block(tenant_id: int, route_id: int = 7, status: str = "active") -> TenantModelBlock:
    return TenantModelBlock(tenant_id=tenant_id, route_id=route_id, status=status)


class TestCreateApiKey:
    async def test_returns_plaintext_and_stores_hash_plus_encrypted(self):
        db = FakeSession()
        db.preload(_route("qwen3-8b"))
        redis = FakeRedis()

        key, plaintext = await create_api_key(
            db,
            redis,
            tenant_id=None,
            name="platform-key",
            allowed_models=["qwen3-8b"],
        )

        assert plaintext.startswith("sk-") and len(plaintext) > 20
        assert key.key_hash == hashlib.sha256(plaintext.encode()).hexdigest()
        # 明文另以可逆加密落库：密文 ≠ 明文，property 解密回显 == 原文
        assert key.key_plaintext_encrypted and key.key_plaintext_encrypted != plaintext
        assert key.plaintext == plaintext
        assert key.tenant_id is None
        assert key.allowed_models == ["qwen3-8b"]
        assert key.status == "active"

    async def test_tenant_key_within_available_models_passes(self):
        db = FakeSession()
        db.preload(_route("qwen3-8b"))
        # 默认放行：无封禁即可签发
        key, plaintext = await create_api_key(
            db,
            FakeRedis(),
            tenant_id=3,
            name="tenant-key",
            allowed_models=["qwen3-8b"],
        )
        assert key.tenant_id == 3
        assert key.status == "active"

    async def test_tenant_key_blocked_model_rejected(self):
        db = FakeSession()
        db.preload(_route("qwen3-8b"))
        # 租户 3 的 route 7 被 active 封禁；尝试为被封禁模型签发
        db.preload(_block(tenant_id=3))

        with pytest.raises(LLMPodValidationError):
            await create_api_key(
                db,
                FakeRedis(),
                tenant_id=3,
                name="bad",
                allowed_models=["qwen3-8b"],
            )
        # 失败时不得落库
        assert db.all(ApiKey) == []

    async def test_tenant_key_disabled_block_passes(self):
        db = FakeSession()
        db.preload(_route("qwen3-8b"))
        db.preload(_block(tenant_id=3, status="disabled"))

        key, _ = await create_api_key(
            db,
            FakeRedis(),
            tenant_id=3,
            name="ok",
            allowed_models=["qwen3-8b"],
        )
        assert key.tenant_id == 3

    async def test_platform_key_skips_block_check(self):
        db = FakeSession()
        db.preload(_route("qwen3-8b"))
        # 无任何封禁，平台 Key 仍可签发（管理端直发跳过租户封禁校验）
        key, _ = await create_api_key(
            db,
            FakeRedis(),
            tenant_id=None,
            name="platform",
            allowed_models=["qwen3-8b"],
        )
        assert key.tenant_id is None

    async def test_unknown_route_rejected(self):
        db = FakeSession()
        with pytest.raises(LLMPodValidationError):
            await create_api_key(
                db,
                FakeRedis(),
                tenant_id=None,
                name="bad",
                allowed_models=["no-such-route"],
            )


class TestSetStatusApiKey:
    async def test_disable_flips_status_and_dels_cache(self):
        db = FakeSession()
        redis = FakeRedis()
        key, _ = await create_api_key(
            db,
            redis,
            tenant_id=None,
            name="k",
            allowed_models=[],
        )
        redis.store[f"{APIKEY_CACHE_PREFIX}{key.key_hash}"] = "{}"  # 模拟网关已回填缓存

        disabled = await disable_api_key(db, redis, key_id=key.id)

        assert disabled.status == "disabled"
        assert redis.deleted == [f"{APIKEY_CACHE_PREFIX}{key.key_hash}", f"llmpod:apikey-known:{key.key_hash}"]
        assert f"{APIKEY_CACHE_PREFIX}{key.key_hash}" not in redis.store

    async def test_disable_without_redis_skips_del(self):
        db = FakeSession()
        key, _ = await create_api_key(db, None, tenant_id=None, name="k", allowed_models=[])
        disabled = await disable_api_key(db, None, key_id=key.id)
        assert disabled.status == "disabled"

    async def test_disable_missing_raises_not_found(self):
        with pytest.raises(NotFound):
            await disable_api_key(FakeSession(), FakeRedis(), key_id=999)

    async def test_disable_idempotent_no_repeat_del(self):
        db = FakeSession()
        redis = FakeRedis()
        key, _ = await create_api_key(db, redis, tenant_id=None, name="k", allowed_models=[])

        await disable_api_key(db, redis, key_id=key.id)
        await disable_api_key(db, redis, key_id=key.id)

        assert redis.deleted == [f"{APIKEY_CACHE_PREFIX}{key.key_hash}", f"llmpod:apikey-known:{key.key_hash}"]

    async def test_enable_restores_active_and_dels_cache(self):
        db = FakeSession()
        redis = FakeRedis()
        key, _ = await create_api_key(db, redis, tenant_id=None, name="k", allowed_models=[])
        await disable_api_key(db, None, key_id=key.id)

        enabled = await enable_api_key(db, redis, key_id=key.id)

        assert enabled.status == "active"
        assert redis.deleted == [f"{APIKEY_CACHE_PREFIX}{key.key_hash}"]
        assert f"llmpod:apikey-known:{key.key_hash}" in redis.store


class TestDeleteApiKey:
    async def test_delete_removes_row_and_dels_cache(self):
        db = FakeSession()
        redis = FakeRedis()
        key, _ = await create_api_key(
            db,
            redis,
            tenant_id=None,
            name="k",
            allowed_models=[],
        )
        redis.store[f"{APIKEY_CACHE_PREFIX}{key.key_hash}"] = "{}"

        await delete_api_key(db, redis, key_id=key.id)

        assert redis.deleted == [f"{APIKEY_CACHE_PREFIX}{key.key_hash}", f"llmpod:apikey-known:{key.key_hash}"]
        assert db.all(ApiKey) == []  # 彻底移除（区别于 disable 的保留记录）

    async def test_delete_missing_raises_not_found(self):
        with pytest.raises(NotFound):
            await delete_api_key(FakeSession(), FakeRedis(), key_id=999)


#: 建 Key 时写满的初始值（键即三态可空列），「传 UNSET = 不改」断言以此为基准
_INITIAL = {
    "rpm_limit": 60,
    "tpm_limit": 120,
    "total_token_quota": 1000,
    "budget_limit": Decimal("50"),
    "expires_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
}


async def _key_with_all_limits(db: FakeSession) -> ApiKey:
    key, _ = await create_api_key(db, FakeRedis(), tenant_id=None, name="k", allowed_models=[], **_INITIAL)
    return key


class TestUpdateApiKey:
    async def test_update_limits_dels_cache(self):
        db = FakeSession()
        redis = FakeRedis()
        key, _ = await create_api_key(db, redis, tenant_id=None, name="k", allowed_models=[])

        updated = await update_api_key(
            db,
            redis,
            key_id=key.id,
            rpm_limit=120,
            tpm_limit=999,
            total_token_quota=5000,
        )

        assert (updated.rpm_limit, updated.tpm_limit, updated.total_token_quota) == (120, 999, 5000)
        assert redis.deleted == [f"{APIKEY_CACHE_PREFIX}{key.key_hash}"]  # 变更即 DEL

    async def test_update_none_keeps_fields(self):
        """NOT NULL 列不受三态影响：传 None 仍=不改（name 无处落 null）。"""
        db = FakeSession()
        key, _ = await create_api_key(db, FakeRedis(), tenant_id=None, name="k", allowed_models=[])
        key.rpm_limit = 60

        updated = await update_api_key(db, None, key_id=key.id, name=None, model_token_quotas=None)
        assert updated.name == "k"
        assert updated.rpm_limit == 60

    async def test_update_revalidates_allowed_models(self):
        db = FakeSession()
        key, _ = await create_api_key(db, FakeRedis(), tenant_id=None, name="k", allowed_models=[])

        with pytest.raises(LLMPodValidationError):
            await update_api_key(db, None, key_id=key.id, allowed_models=["ghost"])

    def test_update_clearable_defaults_are_unset(self):
        """可空列默认值必须是 UNSET：默认改回 None 会让「不传」静默变成「清空」。"""
        params = inspect.signature(update_api_key).parameters
        for field in CLEARABLE_KEY_FIELDS:
            assert params[field].default is UNSET, f"{field} 默认值应为 UNSET（未传=不改）"
        for field in ("name", "allowed_models", "model_token_quotas"):
            assert params[field].default is None, f"{field} 是 NOT NULL 列，不经哨兵"

    @pytest.mark.parametrize("field", CLEARABLE_KEY_FIELDS)
    async def test_update_unset_keeps_field(self, field):
        """传 UNSET（等价于不传）= 不改：旧调用方语义在哨兵下仍成立。"""
        db = FakeSession()
        key = await _key_with_all_limits(db)

        updated = await update_api_key(db, None, key_id=key.id, **{field: UNSET})

        assert getattr(updated, field) == _INITIAL[field]  # 缺字段在此 KeyError，与名单同锁

    @pytest.mark.parametrize("field", CLEARABLE_KEY_FIELDS)
    async def test_update_explicit_none_clears_field(self, field):
        """显式 null = 清空为「不限 / 永不过期」——本 bug 的核心修复点。"""
        db = FakeSession()
        key = await _key_with_all_limits(db)

        updated = await update_api_key(db, None, key_id=key.id, **{field: None})

        assert getattr(updated, field) is None


class TestKeyBudget:
    async def test_update_budget_limit_below_spent_rejected(self):
        """「上限 ≥ 已消费」是网关预算冻结闸门的硬前提（违反则下次预扣恒失败），
        租户与平台直发同一条谓词。"""
        db = FakeSession()
        key, _ = await create_api_key(db, FakeRedis(), tenant_id=5, name="k", allowed_models=[])
        key.budget_spent = Decimal("50")

        with pytest.raises(LLMPodValidationError):
            await update_api_key(db, None, key_id=key.id, budget_limit=Decimal("10"))

    async def test_platform_key_budget_limit_floored_by_spent_too(self):
        """平台直发 Key 同样受「上限 ≥ 已消费」下限约束：预算闸门对它同样生效，
        调到已消费以下 = 下一次预扣永远 402。"""
        db = FakeSession()
        key, _ = await create_api_key(db, FakeRedis(), tenant_id=None, name="k", allowed_models=[])
        key.budget_spent = Decimal("50")

        with pytest.raises(LLMPodValidationError):
            await update_api_key(db, None, key_id=key.id, budget_limit=Decimal("10"))

    async def test_update_budget_limit_sets(self):
        db = FakeSession()
        key, _ = await create_api_key(db, FakeRedis(), tenant_id=None, name="k", allowed_models=[])
        updated = await update_api_key(db, None, key_id=key.id, budget_limit=Decimal("100"))
        assert updated.budget_limit == Decimal("100")

    async def test_update_budget_limit_none_clears_despite_spent(self):
        """清空预算上限不受「下限=已消费额」约束：清空后无上限，恒合法；累计用量不动。"""
        db = FakeSession()
        key, _ = await create_api_key(db, FakeRedis(), tenant_id=None, name="k", allowed_models=[])
        key.budget_spent = Decimal("50")
        await update_api_key(db, None, key_id=key.id, budget_limit=Decimal("500"))

        updated = await update_api_key(db, None, key_id=key.id, budget_limit=None)

        assert updated.budget_limit is None
        assert updated.budget_spent == Decimal("50")  # 清上限 ≠ 清零已消费

    async def test_reset_budget_zeroes_spent(self):
        db = FakeSession()
        key, _ = await create_api_key(db, FakeRedis(), tenant_id=None, name="k", allowed_models=[])
        key.budget_spent = Decimal("42")
        reset = await reset_budget(db, None, key_id=key.id)
        assert reset.budget_spent == Decimal("0")
        assert reset.quota_baselines == {}

    async def test_reset_budget_snapshots_ledger_baselines(self):
        """重置定格累计用量为重建基线（按模型分桶）：rebuild 按净增量聚合，
        重置不会在网关重建的「就高回填」中被静默撤销。"""
        db = FakeSession()
        key, _ = await create_api_key(db, FakeRedis(), tenant_id=None, name="k", allowed_models=[])
        db.preload(_ledger_row(id=1, key_id=key.id, model="m1", total=100))
        db.preload(_ledger_row(id=2, key_id=key.id, model="m1", total=50))
        db.preload(_ledger_row(id=3, key_id=key.id, model="m2", total=30))

        reset = await reset_budget(db, None, key_id=key.id)

        assert reset.quota_baselines == {"m1": 150, "m2": 30}

    async def test_reset_budget_clears_key_quota_counters(self):
        """重置连带清 Key 维度配额计数器（key 总量 + key×模型）；模型全局档不动。"""
        db = FakeSession()
        redis = FakeRedis()
        key, _ = await create_api_key(db, redis, tenant_id=None, name="k", allowed_models=[])
        redis.store[f"llmpod:quota:key:{key.id}"] = "1907"
        redis.store[f"llmpod:quota:keymodel:{key.id}:m1"] = "900"
        redis.store["llmpod:quota:model:m1"] = "5000"  # 平台护栏：跨 Key，不随单 Key 重置清

        await reset_budget(db, redis, key_id=key.id)

        assert f"llmpod:quota:key:{key.id}" not in redis.store
        assert f"llmpod:quota:keymodel:{key.id}:m1" not in redis.store
        assert redis.store.get("llmpod:quota:model:m1") == "5000"
