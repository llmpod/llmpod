"""metrics_scraper 单元测试（差分/回绕/攒桶/落行/引擎映射快照，无 PG、无真实 HTTP）。

只验到「攒桶产出 MetricRollup 行对象」与「引擎快照产出 EngineSnapshot」层：
INSERT 依赖 PG（FakeSession 不支持批量 INSERT），落库本身由本地端到端手验覆盖。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from llmpod.apps.proxyserver.metrics import _LATENCY_BUCKETS
from llmpod.apps.webserver.metrics_scraper import (
    EngineSnapshot,
    MetricsScraper,
    _bucket_floor,
)
from llmpod.config.settings import Settings


def _cfg(**ovr) -> Settings:
    base = dict(
        metrics_scrape_targets="http://t1/metrics",
        metrics_scrape_interval_proxy=1,
        metrics_scrape_interval_engine_fast=1,
        metrics_scrape_interval_engine_slow=1,
        metrics_token="tok",
    )
    base.update(ovr)
    return Settings(**base)


def _proxy_text(
    requests: int,
    status: str = "success",
    tokens: int = 0,
    latency_sum: int = 0,
    latency_count: int = 0,
    bucket: int = 0,
) -> str:
    """构造最小 proxyserver /metrics 文本（counter + 直方图子指标）。"""
    le = _LATENCY_BUCKETS[bucket] if bucket < len(_LATENCY_BUCKETS) else "+Inf"
    return (
        "# TYPE llmpod_proxy_requests_total counter\n"
        f'llmpod_proxy_requests_total{{api_key_id="1",model_alias="m1",operation="chat.completions",'
        f'status="{status}",tenant_id="",endpoint_id="",deployment_id="",engine="",upstream_type=""}} {requests}\n'
        "# TYPE llmpod_proxy_tokens_total counter\n"
        f'llmpod_proxy_tokens_total{{api_key_id="1",model_alias="m1",operation="chat.completions",'
        f'kind="total",tenant_id="",endpoint_id="",deployment_id="",engine="",upstream_type=""}} {tokens}\n'
        "# TYPE llmpod_proxy_request_latency_ms histogram\n"
        f'llmpod_proxy_request_latency_ms_bucket{{api_key_id="1",model_alias="m1",operation="chat.completions",'
        f'tenant_id="",endpoint_id="",deployment_id="",engine="",upstream_type="",le="{le}"}} {latency_count}\n'
        f'llmpod_proxy_request_latency_ms_sum{{api_key_id="1",model_alias="m1",operation="chat.completions",'
        f'tenant_id="",endpoint_id="",deployment_id="",engine="",upstream_type=""}} {latency_sum}\n'
        f'llmpod_proxy_request_latency_ms_count{{api_key_id="1",model_alias="m1",operation="chat.completions",'
        f'tenant_id="",endpoint_id="",deployment_id="",engine="",upstream_type=""}} {latency_count}\n'
    )


NOW = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)


class TestDiff:
    def test_first_sample_builds_baseline_no_delta(self):
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", _proxy_text(requests=5), NOW)
        acc = [a for a in s._buckets.values()]
        # 首见样本只建基线：requests 不计增量
        assert all(a.requests == 0 for a in acc)

    def test_second_sample_deltas(self):
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", _proxy_text(requests=5), NOW)
        s._ingest("http://t1/metrics", _proxy_text(requests=8), NOW)
        req_acc = [a for (t, f, _), a in s._buckets.items() if f == "requests"]
        assert len(req_acc) == 1
        assert req_acc[0].requests == 3

    def test_counter_reset_treated_as_from_zero(self):
        """回绕/重启（本次 < 上次）→ 增量 = 本次值。"""
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", _proxy_text(requests=10), NOW)
        s._ingest("http://t1/metrics", _proxy_text(requests=2), NOW)
        req_acc = [a for (t, f, _), a in s._buckets.items() if f == "requests"]
        assert req_acc[0].requests == 2


class TestBucket:
    def test_bucket_floor_aligns_5min(self):
        assert _bucket_floor(NOW).minute % 5 == 0
        assert _bucket_floor(NOW) == datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    def test_bucket_rotation_moves_pending(self):
        """桶边界翻转 → 旧桶转 pending、新桶重开。"""
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", _proxy_text(requests=5), NOW)
        s._ingest("http://t1/metrics", _proxy_text(requests=8), NOW)  # +3 进 12:00 桶
        later = datetime(2026, 8, 28, 12, 5, 10, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=9), later)  # +1 进 12:05 桶
        pending = s._flush_queue[-1]
        assert pending is not None
        bucket_start, buckets = pending
        assert bucket_start == datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        req = [a for (t, f, _), a in buckets.items() if f == "requests"]
        assert req[0].requests == 3
        # 新桶已有 +1
        new_req = [a for (t, f, _), a in s._buckets.items() if f == "requests"]
        assert new_req[0].requests == 1

    def test_to_row_projects_labels(self):
        s = MetricsScraper(_cfg())
        s._ingest(
            "http://t1/metrics", _proxy_text(requests=5, tokens=100, latency_sum=900, latency_count=3, bucket=3), NOW
        )
        s._ingest(
            "http://t1/metrics", _proxy_text(requests=8, tokens=220, latency_sum=2400, latency_count=6, bucket=3), NOW
        )
        # 取 requests 族（含 status 标签）的累计器
        (target, fam, lk), acc = next(((k, v) for k, v in s._buckets.items() if k[1] == "requests"))
        row = s._to_row(target, s._bucket_start, acc)
        assert row.source == "http://t1/metrics"
        assert row.requests == 3
        assert row.status == "success"
        assert row.api_key_id == 1
        assert row.model_alias == "m1"
        # tokens / latency 各族独立累计
        tok = next(v for k, v in s._buckets.items() if k[1] == "tokens" and v.total_tokens)
        assert tok.total_tokens == 120
        lat = next(v for k, v in s._buckets.items() if k[1] == "llmpod_proxy_request_latency_ms" and v.latency_count)
        assert lat.latency_sum_ms == 1500
        assert lat.latency_count == 3
        assert lat.latency_buckets[3] == 3  # le=100 桶


class TestHistogramParsing:
    def test_bucket_index_matches_bounds(self):
        s = MetricsScraper(_cfg())
        assert s._bucket_index("10", "request_latency_ms") == 0
        assert s._bucket_index("100", "request_latency_ms") == 3
        assert s._bucket_index("+Inf", "request_latency_ms") == len(_LATENCY_BUCKETS)
        assert s._bucket_index("bogus", "request_latency_ms") is None
        assert s._bucket_index(None, "request_latency_ms") is None

    def test_buckets_accumulate_across_scrapes(self):
        """回归：桶计数必须累加（+=），取 max 会丢多轮观测——同一桶内两次
        采样各 +3 观测 → 桶内应为 6 而非 3（分位数以此为分子）。"""
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", _proxy_text(requests=1, latency_count=3, bucket=3), NOW)
        s._ingest("http://t1/metrics", _proxy_text(requests=2, latency_count=6, bucket=3), NOW)
        s._ingest("http://t1/metrics", _proxy_text(requests=3, latency_count=9, bucket=3), NOW)
        lat = next(v for k, v in s._buckets.items() if k[1] == "llmpod_proxy_request_latency_ms" and v.latency_count)
        assert lat.latency_count == 6  # _count 差分累加
        assert lat.latency_buckets[3] == 6  # 回归断言：两轮各 +3 → 6（旧实现 max 后为 3）

    def test_in_flight_gauge_first_and_decreasing_values(self):
        """回归：in_flight 是 gauge，不能走 counter 差分——首见样本与下降值
        都必须记入峰值（旧实现首见=0 被丢、下降被跳过，峰值系统性失真）。"""
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", _proxy_text(requests=1), NOW)
        s._ingest("http://t1/metrics", _proxy_text(requests=2), NOW)
        # 注入 gauge 样本：上升 → 5，下降 → 2
        gauge_text = "# TYPE llmpod_proxy_in_flight_requests gauge\nllmpod_proxy_in_flight_requests {v}\n"
        s._ingest("http://t1/metrics", gauge_text.format(v=5), NOW)
        s._ingest("http://t1/metrics", gauge_text.format(v=2), NOW)
        peaks = [a.in_flight_peak for a in s._buckets.values()]
        assert max(peaks) == 5  # 首见即计（旧实现丢首见）
        assert all(p >= 2 for p in peaks)  # 下降值不破坏已有峰值


class TestIngestIsolation:
    def test_malformed_target_text_does_not_kill_round(self):
        """回归：target 返回非指标文本（如 SPA fallback 的 index.html）→ 只丢该
        target，不抛异常（旧实现 ValueError 冒泡炸掉整轮，引擎抓取/落库全停）。"""
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", "<html>spa index</html>", NOW)  # 不应抛
        s._ingest("http://t1/metrics", _proxy_text(requests=5), NOW)  # 同轮后续照常
        assert any(a.requests >= 0 for a in s._buckets.values())


class TestRejectsStatusLabel:
    def test_rejects_row_carries_status_rejected(self):
        """拒绝行必须带 status=rejected——rejects 样本无该标签，不补则查询层分类不出「被拒」。

        回归：真实环境验证发现 15:15 桶 requests=1 但 rejected 分类为 0（status 列空）。
        """
        s = MetricsScraper(_cfg())
        text = (
            "# TYPE llmpod_proxy_rejects_total counter\n"
            'llmpod_proxy_rejects_total{api_key_id="1",model_alias="m1",operation="chat.completions",'
            'reject_reason="invalid_key",tenant_id="",endpoint_id="",deployment_id="",engine="",upstream_type=""} 3\n'
            "# TYPE llmpod_proxy_requests_total counter\n"
            'llmpod_proxy_requests_total{api_key_id="1",model_alias="m1",operation="chat.completions",'
            'status="rejected",tenant_id="",endpoint_id="",deployment_id="",engine="",upstream_type=""} 3\n'
        )
        s._ingest("http://t1/metrics", text, NOW)
        s._ingest("http://t1/metrics", text.replace("} 3", "} 5"), NOW)  # +2 拒绝
        (target, fam, lk), acc = next(((k, v) for k, v in s._buckets.items() if k[1] == "rejects"))
        row = s._to_row(target, s._bucket_start, acc)
        assert row.requests == 2
        assert row.status == "rejected"  # 关键断言：补齐的 status 标签
        assert row.reject_reason == "invalid_key"
        # requests(status=rejected) 不建行（防双计）
        assert not [k for k in s._buckets if k[1] == "requests"]


VLLM_TEXT = """\
# HELP vllm:gpu_cache_usage_perc Gauge
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc 0.4231
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running 7
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting 2
# TYPE vllm:prefix_cache_hit_rate gauge
vllm:prefix_cache_hit_rate 0.31
"""

# 新版 vLLM（≥0.9.2）改名后的指标名
VLLM_TEXT_NEW = VLLM_TEXT.replace("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc")

SGLANG_TEXT = """\
# TYPE sglang:token_usage gauge
sglang:token_usage 0.55
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs 3
# TYPE sglang:num_queue_reqs gauge
sglang:num_queue_reqs 1
"""


def _target(instance_id="i-1", deployment_id="dep-1", engine="vllm", url="http://10.0.0.5:8000/metrics"):
    from llmpod.apps.webserver.metrics_scraper import _EngineTarget

    return _EngineTarget(url=url, instance_id=instance_id, deployment_id=deployment_id, engine=engine)


class TestEngineIngest:
    def test_vllm_legacy_name_maps_to_snapshot(self):
        """旧版 vllm:gpu_cache_usage_perc（<0.9.2）→ kv_cache_util。"""
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(), VLLM_TEXT, NOW)
        snap = s._engine_snapshots["i-1"]
        assert snap.ok is True
        assert snap.kv_cache_util == pytest.approx(0.4231)
        assert snap.running_requests == 7
        assert snap.waiting_requests == 2
        assert snap.prefix_cache_hit_rate == pytest.approx(0.31)

    def test_vllm_new_name_maps_to_snapshot(self):
        """新版 vllm:kv_cache_usage_perc（≥0.9.2 改名）同样映射（回归：旧版名会导致新版静默抓空）。"""
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(), VLLM_TEXT_NEW, NOW)
        snap = s._engine_snapshots["i-1"]
        assert snap.kv_cache_util == pytest.approx(0.4231)

    def test_sglang_mapping(self):
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(engine="sglang"), SGLANG_TEXT, NOW)
        snap = s._engine_snapshots["i-1"]
        assert snap.kv_cache_util == pytest.approx(0.55)
        assert snap.running_requests == 3
        assert snap.waiting_requests == 1

    def test_counter_suffix_stripped(self):
        """带 _total 后缀的 counter（如 num_preemptions_total）按基名匹配。"""
        s = MetricsScraper(_cfg())
        text = "# TYPE vllm:num_preemptions_total counter\nvllm:num_preemptions_total 5\n"
        s._ingest_engine_text(_target(), text, NOW)
        assert s._engine_snapshots["i-1"].num_preemptions == 5

    def test_unknown_engine_whitelists_nothing(self):
        """未收录引擎 → 白名单外全丢弃，快照 ok=True 但指标为空。"""
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(engine="triton"), VLLM_TEXT, NOW)
        snap = s._engine_snapshots["i-1"]
        assert snap.ok is True
        assert snap.kv_cache_util is None
        assert snap.running_requests is None

    def test_malformed_text_without_prior_snapshot_leaves_no_entry(self):
        """/metrics 文本非 Prom 格式 → 解析失败 → 不抛、不建快照（从未成功过无值可言）。"""
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(), "<html>bad</html>", NOW)
        assert "i-1" not in s._engine_snapshots

    def test_engine_gauges_into_bucket(self):
        """归一名 gauge 累计进引擎桶（avg/max，samples 计抓取次数）。"""
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(), VLLM_TEXT, NOW)
        s._ingest_engine_text(_target(), VLLM_TEXT.replace("0.4231", "0.6"), NOW)
        acc = s._engine_buckets["i-1"]
        assert acc.samples == 2
        assert acc.avg("kv_cache_util") == pytest.approx(0.5115)
        assert acc.max_of("kv_cache_util") == pytest.approx(0.6)
        row = s._engine_row(s._bucket_start, acc)
        assert row.instance_id == "i-1"
        assert row.deployment_id == "dep-1"
        assert row.engine == "vllm"
        assert row.running_requests_avg == pytest.approx(7.0)
        # 引擎 gauge 不再进 metric_rollup（数值走独立表，避免维度行污染管道表）
        assert not [k for k in s._buckets if k[1] == "engine"]

    def test_engine_preemptions_counter_diff(self):
        """抢占是 counter：桶内按实例差分；回绕（引擎重启）按从零开始口径。"""
        s = MetricsScraper(_cfg())
        txt = "# TYPE vllm:num_preemptions_total counter\nvllm:num_preemptions_total {v}\n"
        s._ingest_engine_text(_target(), txt.format(v=5), NOW)  # 首见只建基线
        assert s._engine_buckets["i-1"].preemptions == 0
        s._ingest_engine_text(_target(), txt.format(v=9), NOW)  # +4
        assert s._engine_buckets["i-1"].preemptions == 4
        s._ingest_engine_text(_target(), txt.format(v=2), NOW)  # 回绕 → 增量=本次
        assert s._engine_buckets["i-1"].preemptions == 6

    def test_engine_bucket_rotation_and_zero_sample_skip(self):
        """桶翻转 → 引擎桶入独立落库队列；全桶没抓到的实例不产行。"""
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(), VLLM_TEXT, NOW)
        later = datetime(2026, 8, 28, 12, 5, 10, tzinfo=timezone.utc)
        s._ingest_engine_text(_target(), VLLM_TEXT, later)
        assert len(s._engine_flush_queue) == 1
        bucket_start, accs = s._engine_flush_queue[0]
        assert bucket_start == datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        assert accs["i-1"].samples == 1  # 旧桶只有第一轮

    def test_snapshots_by_deployment_filter(self):
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(instance_id="i-1"), VLLM_TEXT, NOW)
        s._ingest_engine_text(
            _target(instance_id="i-2", deployment_id="dep-2", url="http://10.0.0.6:8000/metrics"),
            VLLM_TEXT,
            NOW,
        )
        dep1 = s.engine_snapshots_by_deployment("dep-1")
        assert [x.instance_id for x in dep1] == ["i-1"]
        assert all(isinstance(x, EngineSnapshot) for x in dep1)

    def test_parse_failure_keeps_last_ok_snapshot_marked_not_ok(self):
        """先成功后失败：保留上次值（面板能区分「旧值」与「从未有」），ok 翻 False。"""
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(), VLLM_TEXT, NOW)
        s._ingest_engine_text(_target(), "<html>bad</html>", NOW)
        snap = s._engine_snapshots["i-1"]
        assert snap.ok is False
        assert snap.kv_cache_util == pytest.approx(0.4231)  # 上次值保留

    def test_snapshot_fetched_at_is_wall_clock(self):
        """快照时间是墙上时钟（响应侧算 age_ms 用），不再用 monotonic。"""
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(), VLLM_TEXT, NOW)
        assert s._engine_snapshots["i-1"].fetched_at == NOW

    def test_all_engine_snapshots(self):
        s = MetricsScraper(_cfg())
        s._ingest_engine_text(_target(instance_id="i-1"), VLLM_TEXT, NOW)
        s._ingest_engine_text(_target(instance_id="i-2"), VLLM_TEXT, NOW)
        assert {x.instance_id for x in s.all_engine_snapshots()} == {"i-1", "i-2"}


class TestHotLayer:
    """热层：逐轮增量按维度聚合 → 环形缓冲点。"""

    def _round(self, s: MetricsScraper, requests: int, text: str, now) -> None:
        s._ingest("http://t1/metrics", text, now)
        s._close_round(now, ok=True)

    def test_round_aggregates_by_dimension(self):
        s = MetricsScraper(_cfg())
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        self._round(s, 0, _proxy_text(requests=5), t1)  # 首见建基线
        t2 = datetime(2026, 8, 28, 12, 0, 31, tzinfo=timezone.utc)
        self._round(s, 0, _proxy_text(requests=8), t2)  # +3
        series = s.hot_series("__all__")
        assert len(series[""]) == 2
        point = series[""][-1]
        assert point.requests == 3
        assert point.dur == 1.0
        # 模型维度同源聚合（m1 标签非空 → 进 model 构成）
        assert s.hot_series("model")["m1"][-1].requests == 3

    def test_round_error_and_reject_classification(self):
        """错误与拒绝各归各位：error 计 errors、rejects 计 rejected；两者都进总量。"""
        s = MetricsScraper(_cfg())
        rej_text = (
            "# TYPE llmpod_proxy_rejects_total counter\n"
            'llmpod_proxy_rejects_total{api_key_id="",model_alias="m1",operation="chat.completions",'
            'reject_reason="invalid_key",tenant_id="",endpoint_id="",deployment_id="",engine="",upstream_type=""} 0\n'
        )
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        for text in (
            _proxy_text(requests=5),
            _proxy_text(requests=6, status="error"),
            rej_text,
        ):
            s._ingest("http://t1/metrics", text, t1)
        s._close_round(t1, ok=True)
        t2 = datetime(2026, 8, 28, 12, 0, 32, tzinfo=timezone.utc)
        for text in (
            _proxy_text(requests=6),  # success +1
            _proxy_text(requests=7, status="error"),  # error +1
            rej_text.replace("} 0", "} 4"),  # rejected +4
        ):
            s._ingest("http://t1/metrics", text, t2)
        s._close_round(t2, ok=True)
        point = s.hot_series("__all__")[""][-1]
        assert point.requests == 6  # +1 success +1 error +4 rejects（拒绝也是到达过网关的请求）
        assert point.errors == 1
        assert point.rejected == 4

    def test_in_flight_sums_across_targets(self):
        """多 proxyserver 实例的在途 gauge 求和记进 __all__ 点。"""
        s = MetricsScraper(_cfg())
        gauge_text = "# TYPE llmpod_proxy_in_flight_requests gauge\nllmpod_proxy_in_flight_requests {v}\n"
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", gauge_text.format(v=5), t1)
        s._ingest("http://t2/metrics", gauge_text.format(v=2), t1)
        s._close_round(t1, ok=True)
        assert s.hot_series("__all__")[""][-1].in_flight == 7

    def test_key_cap_folds_into_other(self):
        """单维度 key 超上界 → 新 key 并入 __other__，总量不丢。"""
        s = MetricsScraper(_cfg())
        from llmpod.apps.webserver import metrics_scraper as ms

        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        texts = [_proxy_text(requests=5).replace('model_alias="m1"', f'model_alias="{k}"') for k in ("m1", "m2")]
        for text in texts:
            s._ingest("http://t1/metrics", text, t1)
        s._close_round(t1, ok=True)  # 首见建基线
        t2 = datetime(2026, 8, 28, 12, 0, 31, tzinfo=timezone.utc)
        for k, val in (("m1", 8), ("m2", 7)):
            s._ingest(
                "http://t1/metrics", _proxy_text(requests=val).replace('model_alias="m1"', f'model_alias="{k}"'), t2
            )
        s._close_round(t2, ok=True)
        dim = s.hot_series("model")
        assert set(dim) == {"m1", "m2"}
        assert dim["m1"][-1].requests == 3
        assert dim["m2"][-1].requests == 2
        # 第三个 key 超界 → 折叠进 __other__
        cap = ms._HOT_MAX_KEYS
        ms._HOT_MAX_KEYS = 2
        try:
            t3 = datetime(2026, 8, 28, 12, 0, 32, tzinfo=timezone.utc)
            third = _proxy_text(requests=5).replace('model_alias="m1"', 'model_alias="m3"')
            s._ingest("http://t1/metrics", third, t3)
            s._close_round(t3, ok=True)
            t4 = datetime(2026, 8, 28, 12, 0, 33, tzinfo=timezone.utc)
            s._ingest("http://t1/metrics", _proxy_text(requests=9).replace('model_alias="m1"', 'model_alias="m3"'), t4)
            s._close_round(t4, ok=True)
            dim = s.hot_series("model")
            assert "m3" not in dim
            assert dim["__other__"][-1].requests == 4
            # 总量维度不受构成折叠影响
            assert s.hot_series("__all__")[""][-1].requests == 4
        finally:
            ms._HOT_MAX_KEYS = cap

    def test_histogram_round_ring(self):
        """直方图逐轮增量冻结进全局环（滑动窗分位数原料）。"""
        s = MetricsScraper(_cfg())
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=1, latency_count=3, bucket=3), t1)
        s._close_round(t1, ok=True)  # 首见 0：不产直方图点
        t2 = datetime(2026, 8, 28, 12, 0, 31, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=2, latency_count=6, bucket=3), t2)
        s._close_round(t2, ok=True)
        ring = s.hot_histograms()
        assert len(ring) == 1
        ts, lat, ttft = ring[0]
        assert ts == t2
        assert lat[3] == 3

    def test_failed_round_no_point_but_time_advances(self):
        """全 target 失败的轮不产点；下一成功点的 dur 覆盖失败跨度。

        失败轮也不推进 last_round_at——热层 age_ms 必须如实变老，否则故障期间
        前端会标「数据 3 秒前」而曲线已僵住。
        """
        s = MetricsScraper(_cfg())
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=5), t1)
        s._close_round(t1, ok=True)
        t2 = datetime(2026, 8, 28, 12, 0, 35, tzinfo=timezone.utc)
        s._close_round(t2, ok=False)  # 失败轮
        assert s.last_round_at == t1  # 时间基准停在最后一次成功轮
        t3 = datetime(2026, 8, 28, 12, 0, 40, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=9), t3)
        s._close_round(t3, ok=True)  # +4，dur=10 覆盖失败轮
        points = s.hot_series("__all__")[""]
        assert len(points) == 2
        assert points[-1].requests == 4
        assert points[-1].dur == 10.0

    def test_prune_hot_idle_keeps_window_history(self):
        """闲置修剪阈值 ≥ 实时窗口（1h）：窗口内有过流量的 key 不被修剪，
        窗口外（含上一环比窗）静默超时的 key 才清（内存有界）。"""
        s = MetricsScraper(_cfg())
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=5), t1)
        s._close_round(t1, ok=True)  # 首见建基线
        t2 = datetime(2026, 8, 28, 12, 0, 31, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=8), t2)  # +3 → m1 进热层
        s._close_round(t2, ok=True)
        # 30 分钟静默（在 1h 窗口内）：不得修剪——构成面板还要回看它
        s._prune_hot_idle(t2 + timedelta(minutes=30))
        assert "m1" in s.hot_series("model")
        # 2 小时静默（窗口与环比窗都已滑过）：清
        s._prune_hot_idle(t2 + timedelta(hours=2))
        assert s.hot_series("model") == {}


class TestDemandTier:
    """引擎档位按需升降：触碰 → 快档；超过需求窗口 → 慢档。"""

    def test_no_demand_starts_slow(self):
        s = MetricsScraper(_cfg())
        assert s._engine_interval() == s._engine_slow

    def test_touch_demand_switches_to_fast(self):
        s = MetricsScraper(_cfg())
        s.touch_demand()
        assert s._engine_interval() == s._engine_fast

    def test_demand_expires_back_to_slow(self):
        s = MetricsScraper(_cfg())
        s.touch_demand()
        # 直接把上次触碰拨回窗口之外
        s._last_demand -= 60
        assert s._engine_interval() == s._engine_slow


class TestHealth:
    def test_health_reports_targets_and_backlog(self):
        s = MetricsScraper(_cfg())
        s._record_health("http://t1/metrics", ok=True, elapsed_ms=12.5)
        s._record_health("http://eng:8000/metrics", ok=False, elapsed_ms=2000.0)
        s._record_health("http://eng:8000/metrics", ok=False, elapsed_ms=2000.0)
        h = s.health()
        proxy = h["proxy_targets"][0]
        assert proxy["consecutive_failures"] == 0
        assert proxy["last_success"] is not None
        assert h["engine_targets"] == []  # 无 DB 发现
        assert h["flush_pending"] == 0
        assert "diff_baseline_keys" in h
        assert "engine_current" in h["intervals"]


class TestScraperFixes:
    """复审修复批的回归测试（per-key dur / 在途清理 / 队列饿死 / 健康键 / 环深）。"""

    def test_sparse_key_dur_spans_silence(self):
        """稀疏 key 的点 dur = 该 key 距上一点的跨度（含静默期），速率不被放大。"""
        s = MetricsScraper(_cfg())
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=5), t1)
        s._close_round(t1, ok=True)
        t2 = datetime(2026, 8, 28, 12, 0, 31, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=8), t2)  # m1 +3
        s._close_round(t2, ok=True)
        # 之后 4 轮 m1 无流量（m2 在动，m1 计数不动）
        other = _proxy_text(requests=1).replace('model_alias="m1"', 'model_alias="m2"')
        for i, req in enumerate((2, 3, 4, 5), start=1):
            ts = datetime(2026, 8, 28, 12, 0, 31 + i, tzinfo=timezone.utc)
            s._ingest("http://t1/metrics", other.replace("} 1", f"}} {req}"), ts)
            s._close_round(ts, ok=True)
        # m1 再次有增量：dur 应覆盖 5s 静默，而不是最后一轮的 1s
        t7 = datetime(2026, 8, 28, 12, 0, 36, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=20), t7)  # m1 +12
        s._close_round(t7, ok=True)
        m1 = s.hot_series("model")["m1"]
        assert m1[-1].requests == 12
        assert m1[-1].dur == 5.0  # t7 - t2

    async def test_in_flight_cleared_when_target_fails(self):
        """抓取失败的 proxy target 清掉其在途残值——下线实例不再永久计入总量。"""
        s = MetricsScraper(_cfg())

        class _BoomClient:
            async def get(self, *_a, **_k):
                raise RuntimeError("down")

        s._in_flight_by_target["http://t1/metrics"] = 7
        await s._scrape_proxy_round(_BoomClient(), datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc))
        assert "http://t1/metrics" not in s._in_flight_by_target

    async def test_engine_queue_drains_when_proxy_flush_fails(self, monkeypatch):
        """proxy 落库失败不得饿死引擎队列——两张表各自 drain。"""
        from llmpod.apps.webserver.metrics_scraper import _Accum, _EngineAccum

        s = MetricsScraper(_cfg())
        s._last_sweep = time.monotonic()  # 跳过 sweep 的 DB 调用
        proxy_acc = _Accum(labels={})
        proxy_acc.requests = 5
        s._flush_queue.append((NOW, {("http://t1/metrics", "requests", ()): proxy_acc}))
        eacc = _EngineAccum(instance_id="i-1", deployment_id="dep-1", engine="vllm")
        eacc.samples = 2
        s._engine_flush_queue.append((NOW, {"i-1": eacc}))

        calls = {"n": 0}

        class _FailFirstDb:
            def add_all(self, rows):  # 同步方法：add_all 本身不异步，flush 处在协程内直接调
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("proxy flush boom")

        import contextlib

        import llmpod.db as db_mod

        @contextlib.asynccontextmanager
        async def _fake_ctx():
            yield _FailFirstDb()

        monkeypatch.setattr(db_mod, "get_db_context", _fake_ctx)
        monkeypatch.setattr(s, "_prune_diff_baselines", lambda: None)
        monkeypatch.setattr(s, "_prune_hot_idle", lambda now: None)
        await s._maybe_flush(NOW)
        # proxy 队头保留（失败重试），引擎队列已清空
        assert len(s._flush_queue) == 1
        assert len(s._engine_flush_queue) == 0

    async def test_fetch_health_key_for_engine(self):
        """引擎抓取的健康按 instance_id 记（URL 端口复用会漂移到新实例）。"""
        from llmpod.apps.webserver.metrics_scraper import _EngineTarget

        s = MetricsScraper(_cfg())

        class _BoomClient:
            async def get(self, *_a, **_k):
                raise RuntimeError("down")

        t = _EngineTarget(url="http://10.0.0.5:8000/metrics", instance_id="i-9", deployment_id="d", engine="vllm")
        await s._scrape_engine_one(_BoomClient(), t, NOW)
        assert "i-9" in s._target_health
        assert s._target_health["i-9"].consecutive_failures == 1
        assert "http://10.0.0.5:8000/metrics" not in s._target_health

    async def test_discovery_prunes_stale_health(self, monkeypatch):
        """实例下线后其健康项被清；proxy URL 的健康保留。"""
        from llmpod.apps.webserver.metrics_scraper import EngineSnapshot

        s = MetricsScraper(_cfg())
        s._record_health("http://t1/metrics", ok=True, elapsed_ms=5.0)
        s._record_health("i-dead", ok=False, elapsed_ms=100.0)
        # 先让 i-dead 出现在快照里（stale 清理以快照/发现集为准）
        s._engine_snapshots["i-dead"] = EngineSnapshot(instance_id="i-dead", deployment_id="d")

        inst = SimpleNamespace(
            instance_id="i-live",
            deployment_id="d",
            status="RUNNING",
            endpoint="http://10.0.0.5:8000",  # head 实例：有端点才值得抓 /metrics
            spec={"port": 8000},
        )
        rows = [(inst, "10.0.0.5", "vllm")]

        import contextlib

        import llmpod.db as db_mod

        class _Db:
            async def execute(self, _stmt):
                return SimpleNamespace(all=lambda: rows)

        @contextlib.asynccontextmanager
        async def _fake_ctx():
            yield _Db()

        monkeypatch.setattr(db_mod, "get_db_context", _fake_ctx)
        out = await s._discover_engine_targets()
        assert set(out) == {"i-live"}
        assert "i-dead" not in s._target_health
        assert "http://t1/metrics" in s._target_health

    async def test_discovery_skips_headless_worker(self, monkeypatch):
        """PP 多机 headless worker：spec 有端口但 endpoint 为空（无 API server 也就没有
        /metrics）——按 endpoint 判定跳过，不再每轮白抓、把 scrape-health 挂成永红。"""
        s = MetricsScraper(_cfg())
        head = SimpleNamespace(
            instance_id="i-head",
            deployment_id="d",
            status="READY",
            endpoint="http://10.0.0.5:8000",
            spec={"port": 8000},
        )
        worker = SimpleNamespace(
            instance_id="i-worker",
            deployment_id="d",
            status="READY",
            endpoint=None,
            spec={"port": 8000},
        )
        rows = [(head, "10.0.0.5", "vllm"), (worker, "10.0.0.6", "vllm")]

        import contextlib

        import llmpod.db as db_mod

        class _Db:
            async def execute(self, _stmt):
                return SimpleNamespace(all=lambda: rows)

        @contextlib.asynccontextmanager
        async def _fake_ctx():
            yield _Db()

        monkeypatch.setattr(db_mod, "get_db_context", _fake_ctx)
        out = await s._discover_engine_targets()
        assert set(out) == {"i-head"}

    def test_ring_maxlen_covers_retention_at_1s_interval(self):
        """环深满足保留时长全覆盖：1s 间隔下 2h 需要 ≥7200 点（旧 4000 上限会截成 66min）。"""
        s = MetricsScraper(
            _cfg(
                metrics_scrape_interval_proxy=1,
                metrics_scrape_interval_engine_fast=1,
                metrics_scrape_interval_engine_slow=1,
            )
        )
        assert s._ring_maxlen >= 7200
        s5 = MetricsScraper(
            _cfg(
                metrics_scrape_interval_proxy=5,
                metrics_scrape_interval_engine_fast=5,
                metrics_scrape_interval_engine_slow=30,
            )
        )
        assert s5._ring_maxlen == 1441

    def test_dim_ring_depth_is_window_not_retention(self):
        """构成维度环只服务当前 1h 窗口（环比只读 __all__），深度取半份保留
        ——满深度时 56 key 形态下约一半热层内存是永远读不到的点。"""
        s5 = MetricsScraper(
            _cfg(
                metrics_scrape_interval_proxy=5,
                metrics_scrape_interval_engine_fast=5,
                metrics_scrape_interval_engine_slow=30,
            )
        )
        assert s5._dim_ring_maxlen == 722  # 1h + 2 拍余量 @5s 点距
        assert s5._ring_maxlen == 2 * 3600 // 5 + 1  # __all__/直方图仍满 2h


class TestReviewFixes:
    """二次复审修复批的回归测试（基线闲置修剪 / 空 dict 哨兵 / 席位顶替 / 首点 dur）。"""

    def test_prune_keeps_baselines_across_proxy_gap(self):
        """基线缺席几轮不算消失：按闲置时长修剪，漏抓/节拍交错后恢复仍出差分。

        回归：旧实现按「本轮是否出现」修剪，proxy 档 > 引擎快档时两个 proxy
        轮之间夹引擎节拍 → 每轮全清 → 增量永远按首见归零且无法自愈。
        """
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", _proxy_text(requests=5), NOW)  # 建基线
        # 两个 proxy 轮之间的引擎节拍：_maybe_flush 照跑、proxy 键本轮缺席
        s._prune_diff_baselines()
        assert s._last  # 不清
        s._ingest("http://t1/metrics", _proxy_text(requests=9), NOW + timedelta(seconds=5))
        req_acc = [a for (t, f, _), a in s._buckets.items() if f == "requests"]
        assert req_acc[0].requests == 4  # 基线还在 → 差分正常

    def test_prune_clears_stale_baselines_by_idle_time(self):
        """长时间（> 阈值）不再出现的键仍被清——churn 防泄漏的目标不丢。"""
        s = MetricsScraper(_cfg())
        s._ingest("http://t1/metrics", _proxy_text(requests=5), NOW)
        from llmpod.apps.webserver import metrics_scraper as ms

        idle = ms._DIFF_BASELINE_IDLE_SECONDS + 10
        for key in s._last:
            val, _seen = s._last[key]
            s._last[key] = (val, time.monotonic() - idle)
        s._prune_diff_baselines()
        assert not s._last

    async def test_discovery_db_failure_returns_none(self, monkeypatch):
        """DB 抖动（查询异常）→ None 哨兵：快照不清（last-known-good 保留）。"""
        import contextlib

        import llmpod.db as db_mod

        s = MetricsScraper(_cfg())
        s._engine_targets["i-1"] = _target()
        s._engine_snapshots["i-1"] = EngineSnapshot(instance_id="i-1", deployment_id="dep-1")

        @contextlib.asynccontextmanager
        async def _boom_ctx():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr(db_mod, "get_db_context", _boom_ctx)
        assert await s._discover_engine_targets() is None  # 失败 ≠ 空
        assert "i-1" in s._engine_snapshots

    async def test_discovery_all_stopped_returns_empty_and_clears(self, monkeypatch):
        """查询成功但 0 实例（部署全停）→ 空 dict：快照/抢占基线一并清。"""
        import contextlib

        import llmpod.db as db_mod

        s = MetricsScraper(_cfg())
        s._engine_targets["i-1"] = _target()
        s._engine_snapshots["i-1"] = EngineSnapshot(instance_id="i-1", deployment_id="dep-1")

        class _Db:
            async def execute(self, _stmt):
                return SimpleNamespace(all=lambda: [])

        @contextlib.asynccontextmanager
        async def _empty_ctx():
            yield _Db()

        monkeypatch.setattr(db_mod, "get_db_context", _empty_ctx)
        assert await s._discover_engine_targets() == {}
        assert "i-1" not in s._engine_snapshots

    async def test_scrape_engines_applies_discovery_result(self, monkeypatch):
        """调用方接线：None（抖动）保留旧 target 集；空 dict（全停）清空——
        空集不清会永续抓已删实例，健康项列出幻影失败。"""

        class _BoomClient:
            async def get(self, *_a, **_k):
                raise RuntimeError("down")

        s = MetricsScraper(_cfg())
        s._engine_targets["i-1"] = _target()

        async def _fail():
            return None

        monkeypatch.setattr(s, "_discover_engine_targets", _fail)
        s._last_discovery = 0.0
        await s._scrape_engines(_BoomClient(), NOW)
        assert "i-1" in s._engine_targets

        async def _empty():
            return {}

        monkeypatch.setattr(s, "_discover_engine_targets", _empty)
        s._last_discovery = 0.0
        await s._scrape_engines(_BoomClient(), NOW)
        assert s._engine_targets == {}

    def test_hot_seat_evicts_silent_key_for_newcomer(self):
        """席位满员时新 key 顶替静默超阈值的在座 key——先到先得永不淘汰会让
        真正吃流量的 key 永远压在 __other__ 里，消耗 Top5 是错的。"""
        from llmpod.apps.webserver import metrics_scraper as ms

        s = MetricsScraper(_cfg())
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        s._ingest("http://t1/metrics", _proxy_text(requests=5), t1)
        s._close_round(t1, ok=True)
        cap = ms._HOT_MAX_KEYS
        evict = ms._HOT_EVICT_IDLE
        ms._HOT_MAX_KEYS = 1
        ms._HOT_EVICT_IDLE = 10.0
        try:
            t2 = t1 + timedelta(seconds=1)
            s._ingest("http://t1/metrics", _proxy_text(requests=8), t2)
            s._close_round(t2, ok=True)  # m1 在座（首点 dur=轮长）
            # m1 静默超过阈值后，新 key m2 顶替其席位
            t3 = t1 + timedelta(seconds=30)
            m2 = _proxy_text(requests=3).replace('model_alias="m1"', 'model_alias="m2"')
            s._ingest("http://t1/metrics", m2, t3)
            s._close_round(t3, ok=True)
            t4 = t1 + timedelta(seconds=31)
            s._ingest("http://t1/metrics", m2.replace("} 3", "} 7"), t4)
            s._close_round(t4, ok=True)
            dim = s.hot_series("model")
            assert "m2" in dim  # 顶替成功
            assert dim["m2"][-1].requests == 4
        finally:
            ms._HOT_MAX_KEYS = cap
            ms._HOT_EVICT_IDLE = evict

    def test_hot_seat_active_holder_not_evicted(self):
        """在座 key 都活跃（静默 < 阈值）时不换座——新 key 折进 __other__ 稳定。"""
        from llmpod.apps.webserver import metrics_scraper as ms

        s = MetricsScraper(_cfg())
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        m1 = _proxy_text(requests=5)
        s._ingest("http://t1/metrics", m1, t1)
        s._close_round(t1, ok=True)
        cap = ms._HOT_MAX_KEYS
        ms._HOT_MAX_KEYS = 1
        try:
            t2 = t1 + timedelta(seconds=1)
            s._ingest("http://t1/metrics", _proxy_text(requests=8), t2)  # m1 活跃
            s._close_round(t2, ok=True)
            t3 = t1 + timedelta(seconds=2)
            m2 = _proxy_text(requests=3).replace('model_alias="m1"', 'model_alias="m2"')
            s._ingest("http://t1/metrics", m2, t3)
            s._close_round(t3, ok=True)  # m2 首见只建基线
            t4 = t1 + timedelta(seconds=3)
            s._ingest("http://t1/metrics", m2.replace("} 3", "} 5"), t4)
            s._close_round(t4, ok=True)  # m2 +2 → 满员且 m1 活跃 → 折进 __other__
            dim = s.hot_series("model")
            assert "m1" in dim  # 活跃者不被顶替
            assert dim["__other__"][-1].requests == 2
        finally:
            ms._HOT_MAX_KEYS = cap

    def test_new_key_first_point_dur_is_round_length(self):
        """新 key 首点 dur = 本轮长度（增量在本轮内累积）——不是 0（整窗分母
        会让速率失真）也不是静默期（它此前根本没有序列）。"""
        s = MetricsScraper(_cfg())
        t1 = datetime(2026, 8, 28, 12, 0, 30, tzinfo=timezone.utc)
        m2 = _proxy_text(requests=5).replace('model_alias="m1"', 'model_alias="m2"')
        s._ingest("http://t1/metrics", m2, t1)
        s._close_round(t1, ok=True)  # m2 首见只建基线
        t2 = t1 + timedelta(seconds=5)
        s._ingest("http://t1/metrics", m2.replace("} 5", "} 8"), t2)
        s._close_round(t2, ok=True)  # m2 +3 → 首点 dur 应为本轮长度
        assert s.hot_series("model")["m2"][-1].dur == 5.0

    def test_engine_nan_gauge_filtered(self):
        """引擎 /metrics 输出 NaN（文本格式允许）→ 按缺失处理，不进快照/桶
        （int()/JSON 序列化会抛/500，落库会写出 NaN）。"""
        s = MetricsScraper(_cfg())
        text = "# TYPE vllm:num_requests_waiting gauge\nvllm:num_requests_waiting NaN\n"
        s._ingest_engine_text(_target(), text, NOW)
        assert s._engine_snapshots["i-1"].waiting_requests is None
        assert "waiting_requests" not in s._engine_buckets["i-1"].sums

    async def test_sweep_tables_isolated_transactions(self, monkeypatch):
        """回归：两张 rollup 表的保留期清理各自独立事务——引擎表 DELETE 失败
        （如库未跑新迁移）不连坐回滚 metric_rollup 的清理。旧实现两条 DELETE
        同一事务，第二张表缺失时第一张的清理也一起回滚、每小时重试每小时失败。"""
        import contextlib

        import llmpod.db as db_mod

        s = MetricsScraper(_cfg())
        monkeypatch.setattr(time, "monotonic", lambda: 10_000.0)
        s._last_sweep = 0.0  # 触发首轮 sweep

        statements = []

        class _Db:
            async def execute(self, stmt):
                statements.append(stmt)
                if "metric_engine_rollup" in str(stmt):
                    raise RuntimeError("UndefinedTableError")

        @contextlib.asynccontextmanager
        async def _fake_ctx():
            yield _Db()

        monkeypatch.setattr(db_mod, "get_db_context", _fake_ctx)
        await s._maybe_sweep(NOW)
        # 引擎表失败后 proxy 表的清理仍在（两个事务、两条语句都发出）
        assert len(statements) == 2
        assert "metric_rollup" in str(statements[0])
        assert "metric_engine_rollup" in str(statements[1])
        assert s._last_sweep > 0  # 时间照常推进：持久失败按小时节拍重试


class TestScraperSingletonLifecycle:
    """start_metrics_scraper_task：单例 _scraper 由任务自身发布/收回。"""

    async def test_singleton_published_and_cleared_by_task(self, monkeypatch):
        import asyncio

        from llmpod.apps.webserver import metrics_scraper as ms
        from llmpod.core.taskwatch import BackgroundTaskManager

        entered = asyncio.Event()

        scraper = MetricsScraper(_cfg())

        async def fake_run(self):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(ms.MetricsScraper, "run", fake_run)
        monkeypatch.setattr(ms, "MetricsScraper", lambda cfg: scraper)

        background_tasks = BackgroundTaskManager()
        await ms.start_metrics_scraper_task(background_tasks, _cfg())
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert ms.get_scraper() is scraper
        await background_tasks.stop()
        assert ms.get_scraper() is None

    async def test_ctor_failure_degrades_without_registering(self, monkeypatch, caplog):
        import logging

        from llmpod.apps.webserver import metrics_scraper as ms
        from llmpod.core.taskwatch import BackgroundTaskManager

        def boom(cfg):
            raise RuntimeError("pg not initialized")

        monkeypatch.setattr(ms, "MetricsScraper", boom)
        background_tasks = BackgroundTaskManager()
        with caplog.at_level(logging.WARNING):
            await ms.start_metrics_scraper_task(background_tasks, _cfg())
        assert not background_tasks._tasks
        assert any("disabled" in r.message for r in caplog.records)
        await background_tasks.stop()
