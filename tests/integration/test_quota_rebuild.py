"""配额计数器 PG 重建：Redis 丢失时从 request_log 调用日志聚合。"""

from __future__ import annotations

from datetime import datetime, timezone

from _fakes import FakeSession

from llmpod.apps.proxyserver.limits import rebuild_quota_counters
from llmpod.models import RequestLog


def _row(key_id=1, model="m1", total=10, id=1) -> RequestLog:
    return RequestLog(
        id=id,
        request_id=f"r{id}",
        tenant_id=None,
        api_key_id=key_id,
        model_alias=model,
        served_model=None,
        operation="chat.completions",
        protocol="openai",
        engine=None,
        upstream_type="local",
        provider_name=None,
        endpoint_id=None,
        retry_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=total,
        cost=None,
        latency_ms=None,
        status="success",
        http_status=200,
        request_ref=None,
        response_ref=None,
        conv_prefix_hash=None,
        truncated=False,
        snapshot_disabled=True,
        error_message=None,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}

    async def set(self, key, value, **kw):
        self.store[key] = int(value)

    async def get(self, key):
        return str(self.store[key]) if key in self.store else None


class TestRebuildQuotaCounters:
    async def test_aggregates_three_dimensions(self):
        db = FakeSession()
        db.preload(_row(id=1, key_id=1, model="m1", total=10))
        db.preload(_row(id=2, key_id=1, model="m1", total=5))
        db.preload(_row(id=3, key_id=1, model="m2", total=7))
        db.preload(_row(id=4, key_id=2, model="m1", total=3))
        redis = FakeRedis()

        counts = await rebuild_quota_counters(db, redis)

        # Key 总量档
        assert redis.store["llmpod:quota:key:1"] == 22
        assert redis.store["llmpod:quota:key:2"] == 3
        # Key×模型档
        assert redis.store["llmpod:quota:keymodel:1:m1"] == 15
        assert redis.store["llmpod:quota:keymodel:1:m2"] == 7
        assert redis.store["llmpod:quota:keymodel:2:m1"] == 3
        # 模型全局档
        assert redis.store["llmpod:quota:model:m1"] == 18
        assert redis.store["llmpod:quota:model:m2"] == 7
        # 写入键数：key 档 2 + key×model 档 3 + model 档 2
        assert counts["keys"] == 7

    async def test_empty_log_writes_nothing(self):
        redis = FakeRedis()
        assert (await rebuild_quota_counters(FakeSession(), redis))["keys"] == 0
        assert redis.store == {}

    async def test_reset_baseline_subtracted_for_key_dimensions(self):
        """重置后的 Key：重建按「累计用量 − 基线」净增量回填 Key 两档；模型全局档全量。

        没有基线扣除时，重建会把重置前的历史用量整个回填（重置被静默撤销）。
        """
        from llmpod.models import ApiKey

        db = FakeSession()
        # key=1 曾用 m1:1000 + m2:500，重置定格基线；重置后又用 m1:100、m2:0
        db.preload(ApiKey(id=1, key_hash="a" * 64, name="k", quota_baselines={"m1": 1000, "m2": 500}))
        db.preload(_row(id=1, key_id=1, model="m1", total=1000))
        db.preload(_row(id=2, key_id=1, model="m2", total=500))
        db.preload(_row(id=3, key_id=1, model="m1", total=100))
        # key=2 从未重置：全量回填
        db.preload(_row(id=4, key_id=2, model="m1", total=3))
        redis = FakeRedis()

        await rebuild_quota_counters(db, redis)

        assert redis.store["llmpod:quota:key:1"] == 100  # 1600 − 1500 基线
        assert redis.store["llmpod:quota:keymodel:1:m1"] == 100  # 1100 − 1000
        assert "llmpod:quota:keymodel:1:m2" not in redis.store  # 500 − 500 = 0，不写
        assert redis.store["llmpod:quota:key:2"] == 3
        # 模型全局档恒全量（跨 Key 护栏，不随单 Key 重置扣减）
        assert redis.store["llmpod:quota:model:m1"] == 1103
        assert redis.store["llmpod:quota:model:m2"] == 500
