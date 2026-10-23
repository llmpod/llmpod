"""TPM/配额原子占位与流式断连估算计费单测。"""

from __future__ import annotations

import asyncio
import pytest

from llmpod.apps.proxyserver.limits import QuotaCounters, RateLimiter, ReservedUsage


class R:
    """incr/incrby/decr/expire/get/set 内存桩（负数允许，模拟真实 Redis）。"""

    def __init__(self):
        self.store: dict[str, int] = {}

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
        return True

    async def get(self, key):
        v = self.store.get(key)
        return str(v) if v is not None else None

    async def set(self, key, value):
        self.store[key] = int(value)


class TestTpmReserve:
    async def test_reserved_usage_carries_request_id_for_reconciliation_log(self):
        """结算失败日志的对账线索：guard 上的 request_id 必须真实可取（旧实现
        getattr 取不存在的属性恒为 None，critical 日志丢失请求关联）。"""
        assert ReservedUsage(request_id="r-abc").request_id == "r-abc"
        assert ReservedUsage().request_id is None  # 内部直调缺省

    async def test_zero_est_new_key_still_gets_ttl(self):
        """est=0 的 INCRBY 0 也会创建计数键，必须同样设 TTL 防永久残留。"""
        r = R()
        rl = RateLimiter(r, clock=lambda: 1000.0)
        seen = {}
        real_expire = r.expire

        async def spy_expire(key, seconds):
            seen[key] = seconds
            return await real_expire(key, seconds)

        r.expire = spy_expire
        await rl.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=0)
        assert r.store.get("llmpod:rl:1:16:tpm") == 0  # 键已创建
        assert seen.get("llmpod:rl:1:16:tpm") == 120  # 且带 2 分钟过期

    async def test_redis_rollback_failure_does_not_replace_original_rejection(self):
        from llmpod.apps.proxyserver.limits import RateLimitError

        class BrokenRollback(R):
            async def incrby(self, key, amount):
                if amount < 0:
                    raise ConnectionError("redis rollback unavailable")
                return await super().incrby(key, amount)

        r = BrokenRollback()
        r.store["llmpod:rl:1:16:rpm"] = 5
        reserve = ReservedUsage()
        with pytest.raises(RateLimitError):
            await RateLimiter(r, clock=lambda: 1000).check(
                key_id=1, rpm_limit=5, tpm_limit=100, est_tokens=30, reserve=reserve
            )
        assert reserve.reserved_any  # 后续 close/release 可重试

    async def test_tpm_atomic_reserve_and_settle(self):
        r = R()
        rl = RateLimiter(r, clock=lambda: 1000.0)
        res = ReservedUsage()
        await rl.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=30, reserve=res)
        assert r.store["llmpod:rl:1:16:tpm"] == 30  # 先占位
        # 结算：真实用量 50 全量加，再归还占位 30 → 净 50
        await rl.record_usage(key_id=1, tokens=50, reserved=res)
        assert r.store["llmpod:rl:1:16:tpm"] == 50

    async def test_tpm_over_limit_rolls_back(self):
        r = R()
        r.store["llmpod:rl:1:16:tpm"] = 90
        rl = RateLimiter(r, clock=lambda: 1000.0)
        from llmpod.apps.proxyserver.limits import RateLimitError

        with pytest.raises(RateLimitError):
            await rl.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=30)
        assert r.store["llmpod:rl:1:16:tpm"] == 90  # 被拒不消耗

    async def test_concurrent_reserves_do_not_read_same_old_value(self):
        """并发窗口：两请求先后占位，第二个看到的是第一个占位后的值（旧实现 GET 后判，
        双双放行超限）。"""
        r = R()
        rl = RateLimiter(r, clock=lambda: 1000.0)
        from llmpod.apps.proxyserver.limits import RateLimitError

        res1, res2 = ReservedUsage(), ReservedUsage()
        await rl.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=60, reserve=res1)
        with pytest.raises(RateLimitError):
            await rl.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=60, reserve=res2)
        assert r.store["llmpod:rl:1:16:tpm"] == 60  # 第二次被拒且已回滚

    async def test_check_then_act_ablation(self):
        key = "llmpod:rl:1:16:tpm"
        naive_redis = R()
        both_read = asyncio.Event()
        reads = 0

        async def naive_check():
            nonlocal reads
            old = int((await naive_redis.get(key)) or 0)
            reads += 1
            if reads == 2:
                both_read.set()
            await both_read.wait()
            if old + 60 <= 100:
                await naive_redis.incrby(key, 60)
                return True
            return False

        assert await asyncio.gather(naive_check(), naive_check()) == [True, True]
        assert naive_redis.store[key] == 120  # 消融：先读后占位，两请求均获准

        fixed_redis = R()
        limiter = RateLimiter(fixed_redis, clock=lambda: 1000.0)
        results = await asyncio.gather(
            limiter.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=60, reserve=ReservedUsage()),
            limiter.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=60, reserve=ReservedUsage()),
            return_exceptions=True,
        )
        from llmpod.apps.proxyserver.limits import RateLimitError

        assert sum(isinstance(result, RateLimitError) for result in results) == 1
        assert fixed_redis.store[key] == 60

    async def test_rpm_rejected_rolls_back_tpm(self):
        r = R()
        r.store["llmpod:rl:1:16:rpm"] = 5  # 已满
        rl = RateLimiter(r, clock=lambda: 1000.0)
        from llmpod.apps.proxyserver.limits import RateLimitError

        res = ReservedUsage()
        with pytest.raises(RateLimitError):
            await rl.check(key_id=1, rpm_limit=5, tpm_limit=100, est_tokens=30, reserve=res)
        assert r.store.get("llmpod:rl:1:16:tpm", 0) == 0  # TPM 占位随 RPM 拒绝归还

    async def test_failure_release_gives_back_all(self):
        r = R()
        rl = RateLimiter(r, clock=lambda: 1000.0)
        res = ReservedUsage()
        await rl.check(key_id=1, rpm_limit=None, tpm_limit=100, est_tokens=30, reserve=res)
        await res.release(r)
        assert r.store["llmpod:rl:1:16:tpm"] == 0


class TestQuotaReserve:
    async def test_quota_reserve_settle_net(self):
        r = R()
        qc = QuotaCounters(r)
        res = ReservedUsage()
        await qc.precheck(
            key_id=1,
            model="m",
            estimated_tokens=100,
            total_token_quota=1000,
            model_token_quota=None,
            model_global_quota=None,
            reserve=res,
        )
        assert r.store["llmpod:quota:key:1"] == 100
        await qc.settle(key_id=1, model="m", tokens=80, reserved=res)  # 真实用量 80
        assert r.store["llmpod:quota:key:1"] == 80  # 净额 = 真实用量

    async def test_quota_rejected_rolls_back(self):
        r = R()
        r.store["llmpod:quota:key:1"] = 950
        qc = QuotaCounters(r)
        from llmpod.apps.proxyserver.limits import QuotaExceededError

        with pytest.raises(QuotaExceededError):
            await qc.precheck(
                key_id=1,
                model="m",
                estimated_tokens=100,
                total_token_quota=1000,
                model_token_quota=None,
                model_global_quota=None,
            )
        assert r.store["llmpod:quota:key:1"] == 950

    async def test_reject_message_reports_true_prior_usage(self):
        """429 文案里的「已用」必须是占位前的真实已用：INCRBY 返回值已含本次
        预估，直接拼会把预估双计（用 950 显示 1050），租户会拿着虚高数字来问。"""
        r = R()
        r.store["llmpod:quota:key:1"] = 950
        qc = QuotaCounters(r)
        from llmpod.apps.proxyserver.limits import QuotaExceededError

        with pytest.raises(QuotaExceededError, match=r"used 950 \+ est 100 > 1000"):
            await qc.precheck(
                key_id=1,
                model="m",
                estimated_tokens=100,
                total_token_quota=1000,
                model_token_quota=None,
                model_global_quota=None,
            )

    async def test_multi_dimension_rejected_rolls_back_earlier_dimensions(self):
        """第二档超限时，第一档已占的位也要回滚。"""
        r = R()
        qc = QuotaCounters(r)
        from llmpod.apps.proxyserver.limits import QuotaExceededError

        # key×model 档限 1000、已用 950：占 100 后 1050 > 1000 → 第二档拒
        r.store["llmpod:quota:keymodel:1:m"] = 950
        with pytest.raises(QuotaExceededError):
            await qc.precheck(
                key_id=1,
                model="m",
                estimated_tokens=100,
                total_token_quota=1000,
                model_token_quota=1000,
                model_global_quota=None,
            )
        assert r.store.get("llmpod:quota:key:1", 0) == 0  # 第一档占位已回滚
        assert r.store["llmpod:quota:keymodel:1:m"] == 950


class TestInterruptedBillingEstimate:
    """流式断连（usage 缺失/半缺）按已交付内容估算，不再按 0 结算。"""

    def test_estimate_text_tokens(self):
        from llmpod.apps.proxyserver.pricing import estimate_text_tokens

        assert estimate_text_tokens("") == 0
        assert estimate_text_tokens("abcd") == 1  # ASCII 4 字符 ≈ 1 token
        assert estimate_text_tokens("你好") == 2  # CJK 1 字 ≈ 1 token

    def test_partial_usage_is_lower_bound_on_interrupted_stream(self):
        from llmpod.apps.proxyserver.inference_handler import _complete_stream_usage

        partial = {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}
        content = "x" * 400  # 100 tokens，模拟 thinking/tool 参数已到达但收尾帧丢失
        # 消融：只检查 output_tokens 是否非零会信任开头占位值 1，少计 99。
        assert _complete_stream_usage(partial, [content], {}, "anthropic.messages", "interrupted") == (
            {
                "input_tokens": 10,
                "prompt_tokens": 10,
                "completion_tokens": 100,
                "total_tokens": 110,
            },
            True,
        )
        assert _complete_stream_usage(partial, [content], {}, "anthropic.messages", "success")[0]["output_tokens"] == 1

    def test_anthropic_start_placeholder_is_not_final_usage(self):
        from llmpod.apps.proxyserver.inference_handler import _complete_stream_usage

        usage = {"input_tokens": 5, "prompt_tokens": 1005, "cache_read_input_tokens": 1000}
        completed, used_estimate = _complete_stream_usage(usage, ["x" * 400], {}, "anthropic.messages", "success")
        assert completed["completion_tokens"] == 100
        assert completed["total_tokens"] == 1105
        assert used_estimate
