"""API 网关限流与配额计数器测试：请求管道第 3/4 级。

- 限流：RPM 固定窗口计数（超即拒）；TPM 同窗（请求前检查，事后按真实 usage 回填）；
  None 上限 = 不限。
- 配额：三档 key（Key 总量 / Key×模型 / 模型全局）INCRBY 计数；请求前
  「已用 + 预估」检查；settle 按真实 usage 落账；配额未配置的档位跳过。
"""

from __future__ import annotations

import pytest

from llmpod.apps.proxyserver.limits import (
    RateLimitError,
    QuotaExceededError,
    QuotaCounters,
    RateLimiter,
)


class FakeRedis:
    """最小 Redis 桩：INCR/INCRBY/EXPIRE/GET 语义（内存 dict）。"""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def incrby(self, key, amount):
        self.store[key] = self.store.get(key, 0) + amount
        return self.store[key]

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def get(self, key):
        v = self.store.get(key)
        return v if v is None else str(v)

    async def set(self, key, value, ex=None):
        self.store[key] = int(value)
        if ex is not None:
            self.ttls[key] = ex
        return True


class TestRateLimiter:
    async def test_rpm_under_limit_passes(self):
        rl = RateLimiter(FakeRedis())
        for _ in range(5):
            await rl.check(key_id=1, rpm_limit=5, tpm_limit=None)  # 不抛即通过

    async def test_rpm_over_limit_rejected(self):
        rl = RateLimiter(FakeRedis())
        for _ in range(3):
            await rl.check(key_id=1, rpm_limit=3, tpm_limit=None)
        with pytest.raises(RateLimitError):
            await rl.check(key_id=1, rpm_limit=3, tpm_limit=None)

    async def test_tpm_prefill_rejected(self):
        from llmpod.apps.proxyserver.limits import _window_key
        import time

        redis = FakeRedis()
        window = _window_key(time.time())
        redis.store[f"llmpod:rl:1:{window}:tpm"] = 1000  # 本窗口已用满
        rl = RateLimiter(redis)
        # 占位后判定：1000（已用）+ 5（占位）> 1000 → 拒且占位回滚
        with pytest.raises(RateLimitError):
            await rl.check(key_id=1, rpm_limit=None, tpm_limit=1000, est_tokens=5)
        assert redis.store[f"llmpod:rl:1:{window}:tpm"] == 1000  # 被拒不消耗

    async def test_tpm_stale_window_does_not_block(self):
        """跨窗口自动归零：上个窗口的计数不影响当前窗口（window 内嵌 key）。"""
        from llmpod.apps.proxyserver.limits import _window_key
        import time

        redis = FakeRedis()
        prev = _window_key(time.time()) - 1
        redis.store[f"llmpod:rl:1:{prev}:tpm"] = 1000  # 上个窗口已用满
        rl = RateLimiter(redis)
        await rl.check(key_id=1, rpm_limit=None, tpm_limit=1000)  # 不抛：当前窗口归零

    async def test_rpm_rejections_do_not_consume_window(self):
        """被拒（429）请求不计入 RPM 窗口——固定间隔重试不会被永久锁死。"""
        from llmpod.apps.proxyserver.limits import _window_key
        import time

        redis = FakeRedis()
        rl = RateLimiter(redis)
        window = _window_key(time.time())
        rpm_key = f"llmpod:rl:1:{window}:rpm"
        for _ in range(3):
            await rl.check(key_id=1, rpm_limit=3, tpm_limit=None)
        # 连续 20 次超限重试：计数器不得增长（否则 6/5、16/5 粘性封锁）
        for _ in range(20):
            with pytest.raises(RateLimitError):
                await rl.check(key_id=1, rpm_limit=3, tpm_limit=None)
        assert redis.store[rpm_key] == 3, "被拒请求不应消耗窗口额度"

    async def test_rpm_frees_slot_after_window(self):
        """下一窗口计数归零（window 内嵌 key，expire 随首次放行设置）。"""
        from llmpod.apps.proxyserver.limits import _window_key
        import time

        redis = FakeRedis()
        rl = RateLimiter(redis)
        now = time.time()
        prev = _window_key(now) - 1
        redis.store[f"llmpod:rl:1:{prev}:rpm"] = 99  # 上窗口打满
        await rl.check(key_id=1, rpm_limit=3, tpm_limit=None)  # 当前窗口照常放行
        cur = _window_key(now)
        assert redis.store[f"llmpod:rl:1:{cur}:rpm"] == 1, "当前窗口从 1 计数"
        assert redis.ttls[f"llmpod:rl:1:{cur}:rpm"] == 60, "TTL 随首次放行设置"
        assert redis.store[f"llmpod:rl:1:{prev}:rpm"] == 99, "上窗口计数互不干扰"

    async def test_tpm_rejection_does_not_consume_rpm_slot(self):
        """TPM 拒绝（429）不占 RPM 名额：TPM 检查排在 RPM 占位之前。"""
        from llmpod.apps.proxyserver.limits import _window_key
        import time

        redis = FakeRedis()
        rl = RateLimiter(redis)
        window = _window_key(time.time())
        redis.store[f"llmpod:rl:1:{window}:tpm"] = 1000  # TPM 已用满
        with pytest.raises(RateLimitError):
            await rl.check(key_id=1, rpm_limit=3, tpm_limit=1000)
        rpm_key = f"llmpod:rl:1:{window}:rpm"
        assert rpm_key not in redis.store, "TPM 被拒的请求不应消耗 RPM 窗口名额"

    async def test_none_limits_always_pass(self):
        rl = RateLimiter(FakeRedis())
        for _ in range(100):
            await rl.check(key_id=1, rpm_limit=None, tpm_limit=None)

    async def test_record_usage_increments_tpm(self):
        from llmpod.apps.proxyserver.limits import _window_key
        import time

        redis = FakeRedis()
        rl = RateLimiter(redis)
        await rl.record_usage(key_id=1, tokens=120)
        window = _window_key(time.time())
        assert redis.store[f"llmpod:rl:1:{window}:tpm"] == 120


class TestQuotaCounters:
    async def test_precheck_under_quota_passes(self):
        q = QuotaCounters(FakeRedis())
        await q.precheck(
            key_id=1,
            model="m1",
            estimated_tokens=100,
            total_token_quota=1000,
            model_token_quota=None,
            model_global_quota=None,
        )

    async def test_total_quota_exceeded(self):
        redis = FakeRedis()
        redis.store["llmpod:quota:key:1"] = 950  # 已用
        q = QuotaCounters(redis)
        with pytest.raises(QuotaExceededError) as ei:
            await q.precheck(
                key_id=1,
                model="m1",
                estimated_tokens=100,
                total_token_quota=1000,
                model_token_quota=None,
                model_global_quota=None,
            )
        assert "total" in str(ei.value.reason) or ei.value.reason

    async def test_model_quota_dimension_reported(self):
        redis = FakeRedis()
        redis.store["llmpod:quota:keymodel:1:m1"] = 500
        q = QuotaCounters(redis)
        with pytest.raises(QuotaExceededError) as ei:
            await q.precheck(
                key_id=1,
                model="m1",
                estimated_tokens=10,
                total_token_quota=None,
                model_token_quota=500,
                model_global_quota=None,
            )
        assert "m1" in ei.value.reason  # 错误体注明维度

    async def test_settle_increments_configured_dimensions(self):
        redis = FakeRedis()
        q = QuotaCounters(redis)
        await q.settle(key_id=1, model="m1", tokens=77)
        assert redis.store["llmpod:quota:key:1"] == 77
        assert redis.store["llmpod:quota:keymodel:1:m1"] == 77
        assert redis.store["llmpod:quota:model:m1"] == 77  # 模型全局档始终计数（平台护栏）

    async def test_unlimited_passes_with_full_usage(self):
        q = QuotaCounters(FakeRedis())
        await q.precheck(
            key_id=1,
            model="m1",
            estimated_tokens=10**9,
            total_token_quota=None,
            model_token_quota=None,
            model_global_quota=None,
        )
