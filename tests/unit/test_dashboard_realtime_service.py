"""dashboard_realtime_service 单元测试。

纯函数部分（槽位聚合 / KPI 环比 / 构成折叠 / 降采样上界）直接构造输入验证；
整屏组装（realtime()）用「真 scraper 实例 + 桩 DB」做端到端——热层路径曾有
把 dataclass 喂给只认 dict 的聚合器的缺陷，纯函数测试与整段 stub 的路由测试
都拦不住，这里必须走到真组装。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.services import dashboard_realtime_service as svc
from llmpod.services.dashboard_realtime_service import (
    _MAX_TREND_POINTS,
    cold_slot_seconds,
    engine_kpis,
    fold_composition,
    hot_slot_seconds,
    key_slot_requests,
    key_slot_tpm,
    merge_agg_rows_by_bucket,
    slot_agg_rows,
    slot_hot_points,
    trend_payload,
)
from llmpod.services.dashboard_realtime_service import _DASHBOARD_HOUR_MAX_SPAN_SECONDS as _DASHBOARD_HOURLY_LIMIT
from llmpod.services.metric_query_service import bucket_seconds_for, local_offset_seconds
from llmpod.utils.time import now_local

TZ = timezone.utc
NOW = datetime(2026, 9, 11, 14, 32, 7, tzinfo=TZ)


def _point(ts: datetime, dur=5.0, **kw) -> SimpleNamespace:
    base = dict(
        requests=0,
        errors=0,
        rejected=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cached_tokens=0,
        latency_sum_ms=0,
        latency_count=0,
        ttft_sum_ms=0,
        ttft_count=0,
        in_flight=0,
    )
    base.update(kw)
    return SimpleNamespace(ts=ts, dur=dur, **base)


def _agg_row(bucket_start: datetime, dim_key=None, **kw) -> SimpleNamespace:
    """SQL 聚合行替身（errors/rejected 已由 FILTER 算好，一行=一桶×一 key）。

    类型对齐 asyncpg 真实口径：PG 的 sum(bigint) 返回 numeric → Decimal，
    sum(integer) → int。桩用 int 时测不出 Decimal 与 float 分母相除的
    TypeError（曾在 /realtime 冷层 500）。
    """
    base = dict(
        requests=0,
        errors=0,
        rejected=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cached_tokens=0,
        latency_sum_ms=0,
        latency_count=0,
        ttft_sum_ms=0,
        ttft_count=0,
    )
    base.update(kw)
    # 逐字段对齐 asyncpg 真实口径：BigInteger 列的 sum → numeric → Decimal；
    # Integer 列（requests/errors/rejected/latency_count/ttft_count）→ int
    base["prompt_tokens"] = Decimal(base["prompt_tokens"])
    base["completion_tokens"] = Decimal(base["completion_tokens"])
    base["total_tokens"] = Decimal(base["total_tokens"])
    base["cached_tokens"] = Decimal(base["cached_tokens"])
    base["latency_sum_ms"] = Decimal(base["latency_sum_ms"])
    base["ttft_sum_ms"] = Decimal(base["ttft_sum_ms"])
    return SimpleNamespace(bucket_start=bucket_start, dim_key=dim_key, **base)


class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _len_buckets(samples, bounds=None) -> list[int]:
    """累计桶替身（末位 = 有值的总数）——与 SQL 的 jsonb_build_array 同构。"""
    values = [v for v in (samples or []) if v > 0]
    buckets = [sum(1 for v in values if v <= b) for b in (bounds or svc.LEN_BUCKET_BOUNDS)]
    return [*buckets, len(values)]


def _len_avg(samples):
    values = [v for v in (samples or []) if v > 0]
    return (sum(values) / len(values)) if values else None


def _log_row(
    *, cur_side, finish_known=0, limit_truncated=0, prompt=None, completion=None, gen_tokens=None, decode_ms=None
):
    """``load_log_window_stats`` 的一行聚合结果替身。

    长度分布是**两列**（累计桶数组 + 均值），读回端按 ``row.prompt_len_buckets`` /
    ``row.prompt_len_avg`` 取——没有「别名里藏键名」的隐式契约。截断率的分母是
    ``finish_known``（结束原因已知的行数），不是全部落账行。
    """
    return SimpleNamespace(
        cur_side=cur_side,
        finish_known=finish_known,
        limit_truncated=limit_truncated,
        gen_tokens=gen_tokens,
        decode_ms=decode_ms,
        prompt_len_buckets=_len_buckets(prompt),
        prompt_len_avg=_len_avg(prompt),
        generation_len_buckets=_len_buckets(completion),
        generation_len_avg=_len_avg(completion),
    )


def _log_stats(
    *, truncation=(None, None), tps=(None, None), prompt_len=None, generation_len=None
) -> svc.LogWindowStats:
    """``load_log_window_stats`` 的返回替身（测试只挑关心的那一项填）。"""
    return svc.LogWindowStats(
        trunc_cur=truncation[0],
        trunc_prev=truncation[1],
        tps_cur=tps[0],
        tps_prev=tps[1],
        prompt_len=prompt_len,
        generation_len=generation_len,
    )


def _db_returning(*row_sets):
    """按调用次序依次返回各结果集的桩 DB（多查询服务的顺序分发）。"""
    sets = list(row_sets)

    class _Db:
        async def execute(self, _stmt, _params=None):
            return _StubResult(sets.pop(0) if sets else [])

    return _Db()


class _StubDb:
    """execute 全部返回空集（引擎网格为空；聚合查询在冷层测试里单独 monkeypatch）。"""

    async def execute(self, _stmt, _params=None):
        return _StubResult([])


class TestRealtimeFilter:
    """整屏筛选：条件构造 + 原始 SQL 片段（值一律走绑定参数）。"""

    def test_active_only_when_any_dimension_set(self):
        assert not svc.RealtimeFilter().active
        assert svc.RealtimeFilter(model="qwen").active
        assert svc.RealtimeFilter(tenant_id=0).active  # 租户 0 是合法值，不是「没筛」
        assert svc.RealtimeFilter(api_key_id=7).active

    def test_filter_parts_emit_both_forms_once(self):
        """ORM 条件与裸 SQL 片段由同一次枚举产出——加维度只可能同时生效。

        两套写法各写一遍时，「只改一处」会让一半面板筛了、另一半没筛且不报错；
        结构上一起产出后这类分叉不可能发生，这条只锁定两种形式确实都产出了。
        """
        empties = svc._filter_parts(None, "m.")
        assert empties == ([], "", {})
        assert svc._filter_parts(svc.RealtimeFilter(), "m.") == ([], "", {})
        # 空串视为未过滤（?model= 不该变成 model_alias='' 的死条件）
        assert svc._filter_parts(svc.RealtimeFilter(model=""), "m.") == ([], "", {})

        orms, clause, params = svc._filter_parts(svc.RealtimeFilter(model="qwen", tenant_id=2, api_key_id=3), "m.")
        assert {c.left.key for c in orms} == {"model_alias", "tenant_id", "api_key_id"}
        assert clause == " AND m.model_alias = :f_model AND m.tenant_id = :f_tenant AND m.api_key_id = :f_apikey"
        assert params == {"f_model": "qwen", "f_tenant": 2, "f_apikey": 3}


class TestSlotHotPoints:
    def test_aggregates_into_slots_and_filters_window(self):
        points = [
            _point(NOW, requests=2, total_tokens=100),
            _point(NOW + timedelta(seconds=5), requests=3, total_tokens=200, in_flight=4),
            _point(NOW + timedelta(seconds=35), requests=7, total_tokens=300),
            _point(NOW - timedelta(seconds=60), requests=99),  # 窗口外
        ]
        slots = slot_hot_points(points, NOW, NOW + timedelta(minutes=1), 30)
        assert len(slots) == 2
        assert slots[0]["requests"] == 5
        assert slots[0]["total_tokens"] == 300
        assert slots[0]["in_flight_last"] == 4
        assert slots[1]["requests"] == 7

    def test_dur_sums_carries_gap(self):
        """漏抓跨度由点 dur 携带：速率分母是真实覆盖时长。"""
        points = [
            _point(NOW, dur=5.0, requests=1),
            _point(NOW + timedelta(seconds=30), dur=30.0, requests=6),  # 含 25s 漏抓
        ]
        slots = slot_hot_points(points, NOW, NOW + timedelta(minutes=1), 60)
        assert slots[0]["dur"] == 35.0


class TestHotSlotSeconds:
    """热层槽宽：按跨度与上限点数取整到抓取间隔的整数倍。

    显式粒度档已随窗口词表一起删除——粒度由后端按跨度定，不再有第二个权威出处。
    """

    def test_auto_1h_with_5s_gap_is_30s(self):
        assert hot_slot_seconds(3600, 5.0) == 30.0

    def test_auto_respects_round_gap(self):
        # 槽宽不小于点距（抓取间隔 15s 时 auto 至少 15s）
        assert hot_slot_seconds(3600, 15.0) == 30.0
        assert hot_slot_seconds(120, 30.0) == 30.0

    def test_auto_caps_point_count(self):
        slot = hot_slot_seconds(7200, 5.0)
        assert 7200 / slot <= _MAX_TREND_POINTS


class TestColdSlotSeconds:
    """冷层槽宽 = 大屏梯子选出的桶宽，与热力图格宽同源（同一个函数）。"""

    @pytest.mark.parametrize(
        "span,expected",
        [(timedelta(hours=1), 300.0), (timedelta(hours=24), 3600.0), (timedelta(days=7), 86400.0)],
    )
    def test_follows_the_span_ladder(self, span, expected):
        assert cold_slot_seconds(NOW - span, NOW) == expected

    @pytest.mark.parametrize("span", [timedelta(hours=1), timedelta(hours=48), timedelta(days=90)])
    def test_point_count_stays_within_cap(self, span):
        """档位自己保证点数上界——这是删掉那层「超上限再升档」while 的依据。

        梯子最坏档是 90 天 / 1d = 90 点，``_MAX_TREND_POINTS`` 是 120。若哪天把
        1h/1d 的界线往上抬（比如 30 天仍按小时 = 720 点），这条会红。
        """
        assert span.total_seconds() // cold_slot_seconds(NOW - span, NOW) <= _MAX_TREND_POINTS


class TestTrendPayload:
    def test_rates_and_baseline(self):
        slots = slot_hot_points(
            [_point(NOW, dur=30.0, requests=10, total_tokens=600, prompt_tokens=400, completion_tokens=200)],
            NOW,
            NOW + timedelta(minutes=1),
            30,
        )
        trend, tpm_series = trend_payload(slots)
        assert trend["points"][0]["qpm"] == 20.0
        assert trend["points"][0]["tpm"] == 1200.0
        assert trend["points"][0]["prompt_tpm"] == 800.0
        assert trend["baseline"] == {"qpm": 20.0, "tpm": 1200.0}
        assert tpm_series == [1200.0]

    def test_baseline_omits_nonpositive_keys(self):
        """首槽基准 ≤0 的键必须省略——前端据此退回绝对量渲染、隐藏索引切换。"""
        slots = slot_hot_points(
            [_point(NOW, dur=30.0, requests=0, total_tokens=500)],
            NOW,
            NOW + timedelta(minutes=1),
            30,
        )
        trend, _ = trend_payload(slots)
        assert "qpm" not in trend["baseline"]
        assert trend["baseline"]["tpm"] == 1000.0

    def test_empty_points_no_baseline(self):
        trend, tpm_series = trend_payload([])
        assert trend["baseline"] == {}
        assert trend["points"] == []
        assert tpm_series == []


class TestFoldComposition:
    def _grid(self):
        total = slot_hot_points(
            [
                _point(NOW, dur=30.0, total_tokens=600),
                _point(NOW + timedelta(seconds=30), dur=30.0, total_tokens=400),
            ],
            NOW,
            NOW + timedelta(minutes=1),
            30,
        )
        return total

    def test_top5_plus_other(self):
        total = self._grid()
        key_slots = {
            f"m{i}": slot_hot_points(
                [_point(NOW, dur=30.0, total_tokens=100 * (7 - i), requests=7 - i)], NOW, NOW + timedelta(minutes=1), 30
            )
            for i in range(7)
        }
        series = key_slot_tpm(key_slots, total)
        req_series = key_slot_requests(key_slots, total)
        folded = fold_composition(key_slots, series, req_series, total, [1200.0, 800.0])
        keys = [item["key"] for item in folded["items"]]
        assert keys == ["m0", "m1", "m2", "m3", "m4", "__other__"]
        # 其他槽0 = m5+m6 的 tpm 之和（30s 内 200/100 个 token → 400/min + 200/min）
        other = folded["items"][-1]["tpm"][0]["value"]
        assert other == 400.0 + 200.0
        # requests 序列同槽对齐：__other__ = m5+m6 的计数（2+1）
        assert folded["items"][-1]["requests"][0]["value"] == 3
        assert folded["items"][0]["requests"][0]["value"] == 7
        # 总量序列与构成对齐（同下标同 ts）
        assert folded["total"][0]["tpm"] == 1200.0
        assert len(folded["total"]) == len(total)

    def test_fewer_than_top5_no_other(self):
        total = self._grid()
        key_slots = {
            "only": slot_hot_points(
                [_point(NOW, dur=30.0, total_tokens=1000, requests=4)], NOW, NOW + timedelta(minutes=1), 30
            )
        }
        series = key_slot_tpm(key_slots, total)
        req_series = key_slot_requests(key_slots, total)
        folded = fold_composition(key_slots, series, req_series, total, [1200.0, 800.0])
        assert [i["key"] for i in folded["items"]] == ["only"]
        assert folded["items"][0]["requests"][0]["value"] == 4

    def test_capped_other_merges_with_rank_fold(self):
        """热层 key 上界折叠已产生的 __other__ 与 top-5 外再折叠相加不丢量。"""
        total = self._grid()
        key_slots = {
            f"m{i}": slot_hot_points(
                [_point(NOW, dur=30.0, total_tokens=100 * (7 - i))], NOW, NOW + timedelta(minutes=1), 30
            )
            for i in range(7)
        }
        key_slots["__other__"] = slot_hot_points(
            [_point(NOW, dur=30.0, total_tokens=50)], NOW, NOW + timedelta(minutes=1), 30
        )
        series = key_slot_tpm(key_slots, total)
        req_series = key_slot_requests(key_slots, total)
        folded = fold_composition(key_slots, series, req_series, total, [1250.0, 800.0])
        other = folded["items"][-1]
        assert other["key"] == "__other__"
        assert other["tpm"][0]["value"] == 400.0 + 200.0 + 100.0  # m5+m6+热层折叠的 __other__

    def test_missing_slot_filled_zero_and_aligned(self):
        """key 缺槽按 0 补，时间轴与总量槽严格对齐（前端按下标擦洗的前提）。"""
        total = self._grid()
        key_slots = {
            "m1": slot_hot_points(
                [_point(NOW + timedelta(seconds=30), dur=30.0, total_tokens=300)],
                NOW,
                NOW + timedelta(minutes=1),
                30,
            )
        }
        series = key_slot_tpm(key_slots, total)
        assert series[0]["m1"] == 0.0
        assert series[1]["m1"] == 600.0
        req_series = key_slot_requests(key_slots, total)
        folded = fold_composition(key_slots, series, req_series, total, [1200.0, 800.0])
        item = folded["items"][0]
        assert item["tpm"][0]["ts"] == folded["total"][0]["ts"]
        assert item["tpm"][0]["value"] == 0.0
        assert item["requests"][0]["value"] == 0

    def test_zero_dur_slot_key_and_total_agree(self):
        """dur=0 的槽（新 key 首点独占）：构成项与总量同为 0——两个分母一致，
        单 key 占比不会超 100%、堆叠构成与总量线不会对不上。"""
        window = (NOW, NOW + timedelta(minutes=1))
        total = slot_hot_points([_point(NOW, dur=0.0, total_tokens=300)], *window, 30)
        key_slots = {"m1": slot_hot_points([_point(NOW, dur=0.0, total_tokens=300)], *window, 30)}
        trend, tpm_series = trend_payload(total)
        series = key_slot_tpm(key_slots, total)
        assert series[0]["m1"] == 0.0
        assert tpm_series[0] == 0.0


class TestSlotAggRows:
    """冷层 SQL 聚合行 → 槽位：dur 按桶计（一行=一桶×一 key），不随行数放大。"""

    def test_rows_grouped_by_bucket(self):
        b0 = datetime(2026, 9, 11, 14, 0, tzinfo=TZ)
        rows = [
            _agg_row(b0, dim_key="m1", requests=10, errors=2, total_tokens=300),
            _agg_row(b0 + timedelta(minutes=5), dim_key="m1", requests=4, errors=4, total_tokens=100),
            _agg_row(b0 + timedelta(minutes=5), dim_key="m2", requests=6, total_tokens=50),
            _agg_row(b0 - timedelta(minutes=10), dim_key="m1", requests=99),  # 窗口外
        ]
        m1 = slot_agg_rows([r for r in rows if r.dim_key == "m1"], b0, b0 + timedelta(minutes=10), 300)
        assert len(m1) == 2
        assert m1[0]["requests"] == 10
        assert m1[0]["errors"] == 2  # SQL FILTER 已算好，槽位不再看 status
        assert m1[0]["dur"] == 300.0
        m2 = slot_agg_rows([r for r in rows if r.dim_key == "m2"], b0, b0 + timedelta(minutes=10), 300)
        assert len(m2) == 1
        assert m2[0]["dur"] == 300.0  # 同桶两个 key 各自的槽 dur 不互相放大

    def test_rate_not_deflated_by_row_count(self):
        """回归：旧实现按「原始桶行」累加 dur，一桶多行时速率被按行数压低。"""
        b0 = datetime(2026, 9, 11, 14, 0, tzinfo=TZ)
        # 同一桶内 9 个标签组合的请求（旧行形态）；聚合后应是一行 9 个请求
        agg = [_agg_row(b0, dim_key="m1", requests=9, total_tokens=900)]
        slot = slot_agg_rows(agg, b0, b0 + timedelta(minutes=5), 300)[0]
        assert slot["dur"] == 300.0  # 不是 300×9
        trend, _ = trend_payload([slot])
        assert trend["points"][0]["qpm"] == pytest.approx(9 / 5)

    def test_decimal_rows_normalized_to_int(self):
        """回归：PG sum(bigint)→numeric，asyncpg 回 Decimal——归一前 Decimal 与
        float 分母相除抛 TypeError，/realtime 冷层（window≠1h 或热层覆盖不足）
        一有数据即 500。"""
        b0 = datetime(2026, 9, 11, 14, 0, tzinfo=TZ)
        slot = slot_agg_rows(
            [_agg_row(b0, dim_key="m1", requests=9, total_tokens=900)],
            b0,
            b0 + timedelta(minutes=5),
            300,
        )[0]
        assert isinstance(slot["total_tokens"], int)
        assert isinstance(slot["latency_sum_ms"], int)
        trend, tpm = trend_payload([slot])
        assert trend["points"][0]["tpm"] == pytest.approx(180.0)  # 900 / 5min
        assert tpm[0] == pytest.approx(180.0)
        # 跨 key 合并路径同口径（merge_agg_rows_by_bucket 喂构成/环比）
        merged = merge_agg_rows_by_bucket([_agg_row(b0, dim_key="m1", total_tokens=900)])
        assert isinstance(merged[0].total_tokens, int)


class TestMergeAggRows:
    def test_sums_keys_including_null(self):
        b0 = datetime(2026, 9, 11, 14, 0, tzinfo=TZ)
        merged = merge_agg_rows_by_bucket(
            [
                _agg_row(b0, dim_key="m1", requests=6, errors=1, total_tokens=100),
                _agg_row(b0, dim_key="m2", requests=4, total_tokens=50),
                _agg_row(b0, dim_key=None, requests=10, total_tokens=25),  # 未选路：进总量不进构成
            ]
        )
        assert len(merged) == 1
        row = merged[0]
        assert row.requests == 20
        assert row.errors == 1
        assert row.total_tokens == 175

    def test_buckets_kept_separate(self):
        b0 = datetime(2026, 9, 11, 14, 0, tzinfo=TZ)
        merged = merge_agg_rows_by_bucket(
            [
                _agg_row(b0, dim_key="m1", requests=1),
                _agg_row(b0 + timedelta(minutes=5), dim_key="m1", requests=2),
            ]
        )
        assert [r.requests for r in merged] == [1, 2]
        assert merged[0].bucket_start < merged[1].bucket_start


class TestEngineKpis:
    def _snap(self, iid, ok=True, kv=0.5, waiting=1):
        return SimpleNamespace(instance_id=iid, ok=ok, kv_cache_util=kv, waiting_requests=waiting)

    def test_busiest_instance_and_queue_sum(self):
        snaps = [
            self._snap("i0", kv=0.4, waiting=0),
            self._snap("i2", kv=0.93, waiting=5),
            self._snap("i1", kv=0.6, waiting=2),
        ]
        kpis = engine_kpis(snaps)
        assert kpis["kv_cache_util"]["value"] == 0.93
        assert kpis["kv_cache_util"]["instance_id"] == "i2"
        assert kpis["waiting_requests"]["value"] == 7

    def test_no_snapshots_null_not_zero(self):
        """无可用快照 → None（前端渲染 --，「没抓到」不等于 0）。"""
        kpis = engine_kpis([])
        assert kpis["kv_cache_util"]["value"] is None
        assert kpis["kv_cache_util"]["instance_id"] is None
        assert kpis["waiting_requests"]["value"] is None

    def test_not_ok_snapshots_excluded(self):
        snaps = [self._snap("i0", ok=False, kv=0.9, waiting=9)]
        kpis = engine_kpis(snaps)
        assert kpis["kv_cache_util"]["value"] is None
        assert kpis["waiting_requests"]["value"] is None

    def test_waiting_unreported_instance_not_counted_as_zero(self):
        """未上报排队数的实例（白名单外引擎/字段改名）不计为 0——只和已知实例，
        与网格单实例卡同口径（那里显示 -- 不显示 0）。"""
        snaps = [self._snap("i0", waiting=None), self._snap("i1", waiting=4)]
        assert engine_kpis(snaps)["waiting_requests"]["value"] == 4

    def test_waiting_all_unreported_is_null(self):
        assert engine_kpis([self._snap("i0", waiting=None)])["waiting_requests"]["value"] is None

    def test_nan_waiting_filtered_not_raising(self):
        """NaN 混进快照不得让 int() 抛 ValueError → /realtime 整屏 500。"""
        snaps = [self._snap("i0", waiting=float("nan")), self._snap("i1", waiting=2)]
        assert engine_kpis(snaps)["waiting_requests"]["value"] == 2


class TestRateKpisDurZero:
    """dur=0 的语义：首点「跨度未知」，不是「整窗覆盖」。"""

    def _total(self, **kw):
        base = svc._empty_slot(NOW)
        base["ts"] = None
        base.update(kw)
        return base

    def test_first_point_with_traffic_uses_round_gap(self):
        """重启后首个有量点按单轮间隔估速率，不塌成 100/3600≈0.03 的近停摆。"""
        cur = self._total(requests=100, total_tokens=60000)
        prev = self._total()
        kpi = svc._rate_kpis(cur, prev, 3600.0, min_span=5.0)
        assert kpi["qps"]["value"] == 20.0
        assert kpi["tpm"]["value"] == 720000.0
        assert kpi["qps"]["delta_pct"] is None  # prev 无数据 → 环比置空

    def test_dur_zero_without_traffic_spans_window(self):
        """全零首点（首见建基线、增量为 0）按整窗摊，速率为 0（窗口空语义）。"""
        kpi = svc._rate_kpis(self._total(), self._total(), 3600.0, min_span=5.0)
        assert kpi["qps"]["value"] == 0.0

    def test_prev_first_point_gets_delta(self):
        """prev 窗口只有 dur=0 首点时也能算出环比（旧实现直接置 None）。"""
        cur = self._total(dur=300.0, requests=12000)  # 40.0 qps
        prev = self._total(requests=100)  # dur=0 → 按 min_span 5s → 20.0 qps
        kpi = svc._rate_kpis(cur, prev, 3600.0, min_span=5.0)
        assert kpi["qps"]["delta_pct"] == 100.0


class TestLoadMergedHistOrdinality:
    """冷层直方图合并的 1-based 序号对位（SQL 需 PG，这里桩掉 execute 只测对位）。"""

    async def test_ordinality_one_based_maps_to_zero_based(self):
        """unnest WITH ORDINALITY 序号从 1 起：idx=4 落 merged[3]、idx=13 的 +Inf 桶保留。

        回归：旧代码 merged[idx] 让累计桶整体右移一格（P95 偏高约一个桶），
        越界的 +Inf 桶被静默丢弃；同一份数据热层/冷层会给出两套 P95。
        """
        rows = [SimpleNamespace(cur_side=True, idx=4, total=40), SimpleNamespace(cur_side=True, idx=13, total=40)]

        class _Db:
            async def execute(self, _stmt, _params=None):
                return _StubResult(rows)

        # 相邻两窗：prev_to == cur_from
        prev_from = NOW - timedelta(hours=1)
        prev_to = NOW
        cur_from = NOW
        cur_to = NOW + timedelta(hours=1)
        cur_hist, prev_hist = await svc.load_merged_hist_pair(_Db(), prev_from, prev_to, cur_from, cur_to)
        assert cur_hist is not None
        assert cur_hist[3] == 40
        assert cur_hist[12] == 40  # +Inf 桶在最后一位，不丢
        assert prev_hist is not None
        assert all(v == 0 for v in prev_hist)

    async def test_side_split_routes_rows_to_windows(self):
        """cur_side 按 bucket_start 归属把行拆进两窗：prev 窗的行不得混进当前窗
        （环比 P95 会拿到错窗样本）。"""
        rows = [
            SimpleNamespace(cur_side=True, idx=3, total=40),
            SimpleNamespace(cur_side=False, idx=3, total=60),
            SimpleNamespace(cur_side=False, idx=13, total=60),
        ]

        class _Db:
            async def execute(self, _stmt, _params=None):
                return _StubResult(rows)

        cur_hist, prev_hist = await svc.load_merged_hist_pair(
            _Db(), NOW - timedelta(hours=2), NOW - timedelta(hours=1), NOW - timedelta(hours=1), NOW
        )
        assert cur_hist[2] == 40
        assert prev_hist[2] == 60
        assert prev_hist[12] == 60

    async def test_empty_window_returns_none(self):
        class _Db:
            async def execute(self, _stmt, _params=None):
                return _StubResult([])

        assert await svc.load_merged_hist_pair(_Db(), NOW - timedelta(hours=1), NOW, NOW, NOW + timedelta(hours=1)) == (
            None,
            None,
        )

    async def test_hot_and_cold_p95_consistent_for_same_histogram(self):
        """同一份直方图经热层（0 基 enumerate）与冷层（1 基序号）合并 → 同一 P95。"""
        lat = [0] * 13
        lat[3] = 40
        lat[12] = 40
        from_ = NOW - timedelta(hours=1)
        to = NOW + timedelta(seconds=1)  # [from, to) 含 NOW 本身
        hot = svc._p95_from_hist_entries([(NOW, lat, None)], from_, to)
        rows = [SimpleNamespace(cur_side=True, idx=i + 1, total=v) for i, v in enumerate(lat)]

        class _Db:
            async def execute(self, _stmt, _params=None):
                return _StubResult(rows)

        cur_hist, _prev = await svc.load_merged_hist_pair(_Db(), from_ - timedelta(hours=1), from_, from_, to)
        cold = svc._p95_or_none(cur_hist)
        assert hot == cold
        assert hot is not None


class TestMergeHistEntries:
    """热层逐轮直方图快照 → 整窗累计桶（与冷层同款按位合并）。"""

    def test_window_filter_and_positional_sum(self):
        # 累计直方图形态：样本落在 (50, 100] → le=100 位及之后每一位都计数
        first_lat = [0, 0, 0, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3]
        first_ttft = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
        second_lat = [0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
        entries = [
            (NOW - timedelta(minutes=1), first_lat, first_ttft),  # 窗外：整轮丢弃
            (NOW, first_lat, first_ttft),
            (NOW + timedelta(minutes=30), second_lat, [0] * 13),
        ]
        lat, ttft = svc._merge_hist_entries(entries, NOW, NOW + timedelta(hours=1))
        assert lat[3] == 5
        assert lat[-1] == 5  # +Inf 位（总量）同样按位相加
        assert ttft[2] == 1
        assert ttft[-1] == 1


class TestRealtimeEndToEnd:
    """整屏组装走真 scraper + 桩 DB（热层路径曾整段 500，这里必须走到真组装）。"""

    def _feed_scraper(self, monkeypatch, *, cover_window=True, hist_counts=None, hist_bucket=7):
        from llmpod.apps.webserver.metrics_scraper import MetricsScraper
        from llmpod.config.settings import Settings

        from tests.unit.test_metrics_scraper import _proxy_text

        cfg = Settings(
            metrics_scrape_targets="http://t1/metrics",
            metrics_scrape_interval_proxy=5,
            metrics_scrape_interval_engine_fast=5,
            metrics_scrape_interval_engine_slow=30,
            metrics_token="tok",
        )
        scraper = MetricsScraper(cfg)
        now = now_local()
        if cover_window:
            # 热层接管要求窗内覆盖 ≥ 半窗：从 40 分钟前起每 5 分钟一轮，
            # 最后两轮有效差分 +3、+4
            stamps = [now - timedelta(minutes=40 - 5 * i) for i in range(9)]
            values = [5, 5, 5, 5, 5, 5, 5, 8, 12]
        else:
            # 冷启动形态：只有最近几秒的点
            stamps = [now - timedelta(seconds=10 * (3 - i)) for i in range(3)]
            values = [5, 8, 12]
        for ts, requests, hist_count in zip(stamps, values, hist_counts or [0] * len(stamps)):
            scraper._ingest("http://t1/metrics", _proxy_text(requests=requests), ts)
            if hist_count:
                # 累计直方图按真实打点形态喂：样本落在 le=bounds[hist_bucket] 位，
                # 则该位及之后每一位都计累计值（只喂单个 le 会让 hist_distribution
                # 读的总量位缺位，分布恒为空）。_diff 按 (target, 样本名, 标签) 建
                # 键，le 是键的一部分 → 同轮多个 le 各自差分互不干扰。
                for idx in range(hist_bucket, len(svc.LATENCY_BUCKET_BOUNDS) + 1):
                    scraper._ingest(
                        "http://t1/metrics",
                        _proxy_text(
                            requests=requests,
                            latency_count=hist_count,
                            latency_sum=hist_count * 2000,
                            bucket=idx,
                        ),
                        ts,
                    )
            scraper._close_round(ts, ok=True)
        monkeypatch.setattr(svc, "get_scraper", lambda: scraper)
        return scraper

    @staticmethod
    def _rolling(hours: float) -> dict:
        """最近 N 小时的显式区间。

        热层用例必须贴着真实 ``now_local()``：喂进 scraper 的点就打在这个时刻，
        锚在整点上会在每小时的某个分钟区间里整体落出区间（用例随钟点随机红）。
        """
        to = now_local()
        return {"from_": to - timedelta(hours=hours), "to": to}

    async def test_hot_path_returns_screen_without_error(self, monkeypatch):
        """回归：热层点（dataclass）曾被喂给只认 dict 的聚合器 → AttributeError 500。"""
        self._feed_scraper(monkeypatch, cover_window=True)
        data = await svc.realtime(_StubDb(), **self._rolling(1), dim="model")
        assert data["window"]["source"] == "hot"
        # KPI 是数值不是异常；__all__ 每轮产点 → 趋势有槽
        assert data["kpi"]["qps"]["value"] >= 0
        assert data["kpi"]["in_flight"]["value"] == 0
        assert data["trend"]["points"]
        assert data["window"]["age_ms"] is not None  # 热层带数据年龄
        # m1 构成来自同一批差分
        assert data["composition"]["dim"] == "model"

    async def test_hot_path_carries_request_log_stats(self, monkeypatch):
        """热层 1h 也产截断率/吐字速度/长度分布（都来自 request_log 落账行）。

        回归：原热分支把截断率恒置 null，注释理由是「热层 30s 轮询不付这次行扫」
        ——但 /anomaly 同为 30s 轮询、扫同一张表（短窗时两边窗口也一样），理由
        不成立；且热/冷回落会让同一窗口的卡片忽隐忽现。
        """
        self._feed_scraper(monkeypatch, cover_window=True)
        captured: dict = {}
        prompt_len = {"buckets": [1, 2, 3], "bounds": [128, 512, None], "avg_tokens": 640}

        async def _fake_log_stats(db, prev_from, prev_to, from_, to, filt=None):
            captured["prev"] = (prev_from, prev_to)
            captured["cur"] = (from_, to)
            return _log_stats(truncation=(0.3, 0.2), tps=(42.5, 31.0), prompt_len=prompt_len)

        monkeypatch.setattr(svc, "load_log_window_stats", _fake_log_stats)
        data = await svc.realtime(_StubDb(), **self._rolling(1), dim="model")
        assert data["window"]["source"] == "hot"
        assert data["kpi"]["truncation_rate"]["value"] == 0.3
        assert data["kpi"]["truncation_rate"]["delta_pp"] == 10.0  # (0.3 - 0.2) × 100
        assert data["kpi"]["output_tps"]["value"] == 42.5
        assert data["kpi"]["output_tps"]["delta_pct"] == 37.1  # (42.5-31)/31 × 100
        assert data["distribution"]["prompt_len"] == prompt_len
        assert data["distribution"]["generation_len"] is None
        # 1h 位移 = 窗长 → 对照窗紧邻当前窗左界
        cur_from, _cur_to = captured["cur"]
        _prev_from, prev_to = captured["prev"]
        assert prev_to == cur_from

    async def test_hot_path_carries_distribution(self, monkeypatch):
        """热层窗口内直方图逐轮增量 → 分布（与 P95 同一份快照产出）。

        1h 是热层的唯一窗口：分布若只在冷层产，屏 2 最常用窗口的分布面板就是
        空态；且热/冷回落在同一窗口上切换时面板会忽隐忽现。
        """
        self._feed_scraper(monkeypatch, cover_window=True, hist_counts=[6 * (i + 1) for i in range(9)], hist_bucket=7)
        data = await svc.realtime(_StubDb(), **self._rolling(1), dim="model")
        assert data["window"]["source"] == "hot"
        dist = data["distribution"]["latency"]
        assert dist is not None
        assert dist["buckets"][-1] == 48  # 首轮建基线，其后 8 轮各 +6 个样本
        assert dist["avg_ms"] == 2000  # 与直方图同一批喂入的 _sum/_count
        assert dist["slow_ratio"] == 0.0  # 样本全在 ≤2500ms 位

    async def test_hot_without_window_coverage_falls_back_to_cold(self, monkeypatch):
        """重启冷启动期热层只有零星几个点（liveness 有、覆盖不足半窗）→ 回落冷层。

        回归：旧判定只看「窗口内存在至少一个点」，重启后第一个点就让 1h 窗口
        走 hot 分支，趋势近乎空白且 source=hot 让前端无法兜底。
        """
        self._feed_scraper(monkeypatch, cover_window=False)
        data = await svc.realtime(_StubDb(), **self._rolling(1), dim="model")
        assert data["window"]["source"] == "cold"
        assert data["window"]["age_ms"] is None

    async def test_past_range_of_hot_span_falls_back_to_cold(self, monkeypatch):
        """跨度 ≤1h 但区间落在过去 → 必须回冷层。

        热层门只看跨度，而内存环只保留最近一段。「拿热层回答历史区间」会返回
        看起来完全正常的错数据，这是本轮最需要锁定的一处：区间整个落在环外时
        覆盖率判定必须把它挡下来。
        """
        self._feed_scraper(monkeypatch, cover_window=True)
        now = now_local()
        data = await svc.realtime(_StubDb(), from_=now - timedelta(hours=3), to=now - timedelta(hours=2), dim="model")
        assert data["window"]["source"] == "cold"

    async def test_hot_path_with_empty_compare_window_yields_null_not_error(self, monkeypatch):
        """热层路径给了对照窗但窗内无点 → delta null，不抛异常。

        回归：热层的上一窗 P95 曾无条件按 (prev_from, prev_to) 过滤，未给对照窗时
        None 传进区间比较直接 TypeError（整屏 500）。
        """
        self._feed_scraper(monkeypatch, cover_window=True)
        now = now_local()
        data = await svc.realtime(
            _StubDb(),
            from_=now - timedelta(minutes=40),
            to=now,
            prev_from=now - timedelta(minutes=120),
            prev_to=now - timedelta(minutes=80),
            dim="model",
        )
        assert data["window"]["source"] == "hot"
        assert data["kpi"]["calls"]["value"] > 0
        assert data["kpi"]["calls"]["delta_pct"] is None

    async def test_filter_forces_cold_path_and_reaches_loaders(self, monkeypatch):
        """带筛选一律走冷层（热层按维度分环存，跨维度筛选取不到），并传到底层查询。

        热层此刻是**可用**的（cover_window=True），所以 source=cold 只可能是因为
        筛选——这正是「模型=A 下按 API Key 拆」这类跨维度筛选拿不到数据时的兜底。
        """
        self._feed_scraper(monkeypatch, cover_window=True)
        captured: dict = {}

        async def _fake_agg(db, from_, to, dim, filt=None):
            captured["filt"] = filt
            return []

        async def _fake_dist(db, from_, to, filt=None):
            return None, None

        monkeypatch.setattr(svc, "load_agg_rows", _fake_agg)
        monkeypatch.setattr(svc, "load_window_histograms", _fake_dist)
        data = await svc.realtime(_StubDb(), **self._rolling(1), dim="api_key", model="qwen", tenant_id=3)
        assert data["window"]["source"] == "cold"
        assert captured["filt"].model == "qwen"
        assert captured["filt"].tenant_id == 3

    async def test_invalid_dim_raises_value_error(self, monkeypatch):
        """非法 dim 抛领域异常（webserver 映射 422）——不静默改写成 model。"""
        monkeypatch.setattr(svc, "get_scraper", lambda: None)
        with pytest.raises(LLMPodValidationError):
            await svc.realtime(_StubDb(), **self._rolling(1), dim="endpoint")

    async def test_cold_path_when_no_scraper(self, monkeypatch):
        """scraper 未启动 → 回落冷层：聚合行分组、在途值置 null、age_ms null。"""
        monkeypatch.setattr(svc, "get_scraper", lambda: None)
        # 窗口是相对 now 的滚动窗：fixture 必须锚在 now 上（锚整点会在每小时的
        # 部分分钟里全部落出窗外，用例随钟点随机红）
        anchor = datetime.now(timezone.utc).astimezone() - timedelta(minutes=30)
        b0 = anchor.replace(minute=anchor.minute - anchor.minute % 5, second=0, microsecond=0)
        rows = [
            _agg_row(
                b0, dim_key="m1", requests=30, errors=3, total_tokens=6000, latency_count=30, latency_sum_ms=21000
            ),
            _agg_row(b0 + timedelta(minutes=5), dim_key="m1", requests=10, total_tokens=2000),
            _agg_row(b0, dim_key=None, requests=5, total_tokens=500),
        ]
        hist = [0] * 13
        hist[3] = 40
        hist[-1] = 40

        async def _fake_agg(db, from_, to, dim, filt=None):
            return rows

        async def _fake_hist(db, prev_from, prev_to, from_, to, filt=None):
            return hist, [0] * len(hist)

        async def _fake_dist(db, from_, to, filt=None):
            # 不给对照窗时当前窗直方图走这条（不再借道 load_merged_hist_pair）
            return hist, [0] * len(hist)

        monkeypatch.setattr(svc, "load_agg_rows", _fake_agg)
        monkeypatch.setattr(svc, "load_merged_hist_pair", _fake_hist)
        monkeypatch.setattr(svc, "load_window_histograms", _fake_dist)
        data = await svc.realtime(_StubDb(), **self._rolling(1), dim="model")
        assert data["window"]["source"] == "cold"
        assert data["kpi"]["in_flight"]["value"] is None  # 瞬时在途只有热层有
        assert data["kpi"]["in_flight"]["peak"] is None  # 桶内峰值量纲不同，一并置空
        assert data["window"]["age_ms"] is None  # 冷层不标热层年龄
        assert data["kpi"]["qps"]["value"] > 0
        # 未选路（NULL 维度）进总量、不进构成
        assert [i["key"] for i in data["composition"]["items"]] == ["m1"]
        # P95 由合并直方图插值（40 样本全在 (100,250] 桶）
        assert data["kpi"]["p95_ms"]["value"] is not None

    async def test_cold_volume_kpis_and_trend_fields(self, monkeypatch):
        """冷层路径的计数类 KPI 与趋势点新字段（requests / latency_avg_ms）。"""
        monkeypatch.setattr(svc, "get_scraper", lambda: None)
        anchor = datetime.now(timezone.utc).astimezone() - timedelta(minutes=30)
        b0 = anchor.replace(minute=anchor.minute - anchor.minute % 5, second=0, microsecond=0)
        rows = [
            _agg_row(
                b0,
                dim_key="m1",
                requests=40,
                total_tokens=80000,
                latency_count=40,
                latency_sum_ms=28000,
                ttft_count=20,
                ttft_sum_ms=6000,
            ),
        ]

        async def _fake_agg(db, from_, to, dim, filt=None):
            return rows

        async def _fake_hist(db, prev_from, prev_to, from_, to, filt=None):
            return None, None

        async def _fake_dist(db, from_, to, filt=None):
            return None, None

        async def _fake_log_stats(db, prev_from, prev_to, from_, to, filt=None):
            return _log_stats(truncation=(0.25, 0.125))

        monkeypatch.setattr(svc, "load_agg_rows", _fake_agg)
        monkeypatch.setattr(svc, "load_merged_hist_pair", _fake_hist)
        monkeypatch.setattr(svc, "load_window_histograms", _fake_dist)
        monkeypatch.setattr(svc, "load_log_window_stats", _fake_log_stats)
        data = await svc.realtime(_StubDb(), **self._rolling(1), dim="model")
        kpi = data["kpi"]
        assert kpi["calls"]["value"] == 40
        assert kpi["total_tokens_kpi"]["value"] == 80000
        assert kpi["avg_context"]["value"] == 2000
        assert kpi["truncation_rate"]["value"] == 0.25
        assert kpi["truncation_rate"]["delta_pp"] == 12.5  # (0.25 - 0.125) × 100
        point = data["trend"]["points"][0]
        assert point["requests"] == 40
        assert point["latency_avg_ms"] == 700  # 28000 / 40
        # 冷层无直方图/落账样本时各分布为空态（键在、值为 null）
        assert data["distribution"] == {
            "latency": None,
            "ttft": None,
            "prompt_len": None,
            "generation_len": None,
        }

    async def test_long_span_cold_daily_granularity(self, monkeypatch):
        """跨度 >48h 的冷层粒度是 1d（跨日就看日，7d = 7 根柱子）。"""
        monkeypatch.setattr(svc, "get_scraper", lambda: None)

        async def _fake_agg(db, from_, to, dim, filt=None):
            return []

        monkeypatch.setattr(svc, "load_agg_rows", _fake_agg)
        for hours in (24 * 7, 24 * 30):
            data = await svc.realtime(_StubDb(), **self._rolling(hours), dim="model")
            assert data["window"]["source"] == "cold"
            assert data["window"]["granularity"] == "1d", hours

    async def test_point_count_never_exceeds_cap(self, monkeypatch):
        """梯子选出的槽宽必须让整窗点数守在 ``_MAX_TREND_POINTS`` 以内。

        显式粒度档已删除，槽宽只由跨度决定——这条锁定「梯子最长一档 ≤48h/1h
        = 48 点」这个上界，改了梯子阈值而没重算点数会在这里红。
        """
        monkeypatch.setattr(svc, "get_scraper", lambda: None)

        async def _fake_agg(db, from_, to, dim, filt=None):
            return []

        monkeypatch.setattr(svc, "load_agg_rows", _fake_agg)
        for hours in (1, 6, 12, 24, 48, 24 * 7, 24 * 90):
            data = await svc.realtime(_StubDb(), **self._rolling(hours), dim="model")
            assert len(data["trend"]["points"]) <= _MAX_TREND_POINTS, hours

    async def test_absent_compare_window_yields_null_deltas(self, monkeypatch):
        """不给对照窗 → 各 delta 全 null（而不是拿某个默认窗冒充）。

        对照语义（今日退整日、滚动窗退一跨）已移到调用方：后端不知道「今日」
        这个概念，也就无从推断该跟哪一段比。
        """
        monkeypatch.setattr(svc, "get_scraper", lambda: None)
        anchor = now_local() - timedelta(days=8)
        b0 = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = [_agg_row(b0, dim_key="m1", requests=100, total_tokens=10000)]

        async def _fake_agg(db, from_, to, dim, filt=None):
            return rows

        async def _fake_hist(db, prev_from, prev_to, from_, to, filt=None):
            return None, None

        async def _fake_dist(db, from_, to, filt=None):
            return None, None

        async def _fake_log_stats(db, prev_from, prev_to, from_, to, filt=None):
            return _log_stats(truncation=(0.1, None))

        monkeypatch.setattr(svc, "load_agg_rows", _fake_agg)
        monkeypatch.setattr(svc, "load_merged_hist_pair", _fake_hist)
        monkeypatch.setattr(svc, "load_window_histograms", _fake_dist)
        monkeypatch.setattr(svc, "load_log_window_stats", _fake_log_stats)
        data = await svc.realtime(_StubDb(), **self._rolling(24 * 7), dim="model")
        assert data["kpi"]["calls"]["delta_pct"] is None
        assert data["kpi"]["truncation_rate"]["delta_pp"] is None

    async def test_compare_window_is_used_verbatim(self, monkeypatch):
        """给了对照窗就照用：不推导、不位移、不因跨度长而忽略。

        今日的「昨日同期」就是调用方把 prev_* 算成昨日同一钟点区间传进来；
        后端做的是原样下推到查询，这条锁定中途没有被二次改写。
        """
        monkeypatch.setattr(svc, "get_scraper", lambda: None)
        captured: dict = {}

        async def _fake_agg(db, from_, to, dim, filt=None):
            captured["agg_from"] = from_
            return []

        async def _fake_hist(db, prev_from, prev_to, from_, to, filt=None):
            captured["prev"] = (prev_from, prev_to)
            captured["cur"] = (from_, to)
            return None, None

        async def _fake_dist(db, from_, to, filt=None):
            return None, None

        async def _fake_log_stats(db, prev_from, prev_to, from_, to, filt=None):
            return _log_stats(truncation=(0.1, 0.2))

        monkeypatch.setattr(svc, "load_agg_rows", _fake_agg)
        monkeypatch.setattr(svc, "load_merged_hist_pair", _fake_hist)
        monkeypatch.setattr(svc, "load_window_histograms", _fake_dist)
        monkeypatch.setattr(svc, "load_log_window_stats", _fake_log_stats)

        to = now_local()
        cur_from = to.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_from, prev_to = cur_from - timedelta(days=1), to - timedelta(days=1)
        data = await svc.realtime(_StubDb(), from_=cur_from, to=to, prev_from=prev_from, prev_to=prev_to, dim="model")

        assert captured["cur"] == (cur_from, to)
        assert captured["prev"] == (prev_from, prev_to)
        # 取行左界从对照窗起点起：两个窗 + 中间隔着昨日剩余时段
        assert captured["agg_from"] == prev_from
        assert data["kpi"]["truncation_rate"]["delta_pp"] == -10.0  # (0.1 - 0.2) × 100

    async def test_half_given_compare_window_raises(self, monkeypatch):
        """只给 prev_from 不给 prev_to → 422：半个对照窗只会静默算出错的 delta。"""
        monkeypatch.setattr(svc, "get_scraper", lambda: None)
        with pytest.raises(LLMPodValidationError, match="prev_from"):
            await svc.realtime(_StubDb(), **self._rolling(1), prev_from=NOW - timedelta(days=1), dim="model")

    async def test_client_clock_ahead_shrinks_compare_window_instead_of_raising(self, monkeypatch):
        """客户端钟快于服务端（to 落在未来被钳回 now）不再报「必须等长」。

        回归：钳位只压短当前窗、对照窗保持调用方给的原样 → 等长校验把钟差报成
        「区间非法」，而调用方给的四个时间戳本身完全等长，大屏每次轮询都红。
        """
        monkeypatch.setattr(svc, "get_scraper", lambda: None)
        captured: dict = {}

        async def _fake_agg(db, from_, to, dim, filt=None):
            return []

        async def _fake_hist(db, prev_from, prev_to, from_, to, filt=None):
            captured["prev"] = (prev_from, prev_to)
            captured["cur"] = (from_, to)
            return None, None

        async def _fake_dist(db, from_, to, filt=None):
            return None, None

        async def _fake_log_stats(db, prev_from, prev_to, from_, to, filt=None):
            return _log_stats(truncation=(0.1, 0.2))

        monkeypatch.setattr(svc, "load_agg_rows", _fake_agg)
        monkeypatch.setattr(svc, "load_merged_hist_pair", _fake_hist)
        monkeypatch.setattr(svc, "load_window_histograms", _fake_dist)
        monkeypatch.setattr(svc, "load_log_window_stats", _fake_log_stats)

        client_now = now_local() + timedelta(seconds=3)
        from_ = client_now - timedelta(hours=1)
        prev_from = from_ - timedelta(hours=1)
        await svc.realtime(
            _StubDb(),
            from_=from_,
            to=client_now,
            prev_from=prev_from,
            prev_to=client_now - timedelta(hours=1),
            dim="model",
        )

        cur_from, cur_to = captured["cur"]
        prev_window = captured["prev"]
        assert cur_to < client_now  # 钳回服务端 now：窗口右端不落在未来
        # 钳位量按同量扣到对照窗右端 → 两窗仍然等长，左端仍是调用方给的锚点
        assert prev_window == (prev_from, cur_to - timedelta(hours=1))
        assert cur_to - cur_from == prev_window[1] - prev_window[0]


class TestVolumeKpis:
    def _total(self, **kw):
        base = svc._empty_slot(NOW)
        base.update(kw)
        return base

    def test_counts_use_forwarded_semantics(self):
        """calls = requests − rejected（触达上游），与 /summary /overview 同名卡同口径。"""
        cur = self._total(requests=1100, rejected=1000, total_tokens=180000)
        prev = self._total(requests=1080, rejected=1000, total_tokens=120000)
        kpi = svc._volume_kpis(cur, prev)
        # 拒绝风暴：到达 1100、触达 100——calls 卡必须显示 100 而非 1100
        assert kpi["calls"] == {"value": 100, "delta_pct": 25.0}  # prev 触达 80
        assert kpi["total_tokens_kpi"] == {"value": 180000, "delta_pct": 50.0}
        assert kpi["avg_context"] == {"value": 1800, "delta": 300}  # 1800 vs 1500

    def test_prev_empty_deltas_null(self):
        """上一窗口无数据：delta 全 null（value 照常给，前端渲染 -- 的只有环比）。"""
        kpi = svc._volume_kpis(self._total(requests=10, total_tokens=100), self._total())
        assert kpi["calls"]["delta_pct"] is None
        assert kpi["total_tokens_kpi"]["delta_pct"] is None
        assert kpi["avg_context"]["delta"] is None

    def test_no_requests_context_null(self):
        kpi = svc._volume_kpis(self._total(), self._total())
        assert kpi["avg_context"]["value"] is None


class TestHistDistribution:
    def test_buckets_bounds_and_slow_ratio(self):
        """cumulative 桶 → 分布载荷：bounds 末位 None=+Inf；slow_ratio = 超阈占比。"""
        lat = [0] * 13
        # LATENCY_BUCKET_BOUNDS = (10,25,50,100,250,500,1000,2500,...)：idx7=≤2500ms
        lat[7] = 9860
        lat[-1] = 10000  # 总 1 万，其中 140 个 >2500ms
        dist = svc.hist_distribution(lat, 710, svc._SLOW_LATENCY_BOUND)
        assert dist["buckets"] is lat
        assert len(dist["bounds"]) == 13
        assert dist["bounds"][-1] is None
        assert dist["avg_ms"] == 710
        assert dist["slow_ratio"] == 0.014

    def test_no_samples_returns_none(self):
        empty = [0] * 13
        assert svc.hist_distribution(empty, None, svc._SLOW_LATENCY_BOUND) is None
        assert svc.hist_distribution(None, 100, svc._SLOW_LATENCY_BOUND) is None

    def test_all_fast_zero_ratio(self):
        lat = [0] * 13
        # cumulative 语义：≤100ms 处 100 个样本后，其后所有累计位（含 2500ms 档）同值
        lat[3] = 100
        lat[4] = lat[5] = lat[6] = lat[7] = 100
        lat[-1] = 100
        dist = svc.hist_distribution(lat, 200, svc._SLOW_LATENCY_BOUND)
        assert dist["slow_ratio"] == 0.0


class TestLoadWindowHistograms:
    async def test_which_split_and_ordinality(self):
        """UNION ALL 双数组行的 which 路由 + 1-based 序号对位（与 hist pair 同坑）。"""
        rows = [
            SimpleNamespace(which="latency", idx=4, total=40),
            SimpleNamespace(which="ttft", idx=4, total=60),
            SimpleNamespace(which="ttft", idx=13, total=60),
        ]
        lat, ttft = await svc.load_window_histograms(_db_returning(rows), NOW - timedelta(hours=1), NOW)
        assert lat is not None and ttft is not None
        assert lat[3] == 40
        assert ttft[3] == 60
        assert ttft[12] == 60  # +Inf 桶保留
        assert sum(lat) == 40

    async def test_empty_returns_none_pair(self):
        assert await svc.load_window_histograms(_StubDb(), NOW - timedelta(hours=1), NOW) == (None, None)


class TestLoadLogWindowStats:
    """一次 request_log 扫描产出的三样东西：截断率 / 长度分布 / 吐字速度。"""

    async def test_truncation_side_split(self):
        """截断率分两窗（当前/对照各一组，KPI 要环比）。"""
        rows = [
            _log_row(cur_side=True, finish_known=200, limit_truncated=10),
            _log_row(cur_side=False, finish_known=100, limit_truncated=25),
        ]
        prev_from = NOW - timedelta(days=1)
        prev_to = NOW - timedelta(days=1) + timedelta(minutes=30)
        stats = await svc.load_log_window_stats(
            _db_returning(rows), prev_from, prev_to, NOW.replace(hour=0, minute=0), NOW
        )
        assert (stats.trunc_cur, stats.trunc_prev) == (0.05, 0.25)

    async def test_sql_takes_both_windows_and_skips_the_gap(self):
        """WHERE 取两窗**并集**，不是 [prev_from, to) 的连续区间。

        ``today`` 的两窗之间隔着昨日剩余时段（14:19 时约四分之一行数），整段扫进来
        再逐行丢弃等于白付聚合代价——所以空档排除在 SQL 层，Python 侧不再有 NULL 侧。
        """
        captured: dict = {}

        class _Db:
            async def execute(self, stmt, params=None):
                captured["sql"] = str(stmt)
                return _StubResult([])

        await svc.load_log_window_stats(_Db(), NOW - timedelta(days=1), NOW - timedelta(days=1), NOW, NOW)
        assert "created_at >= :from_ts AND created_at < :to_ts" in captured["sql"]
        assert "created_at >= :prev_from_ts AND created_at < :prev_to_ts" in captured["sql"]

    async def test_length_hist_is_cumulative_with_total_last(self):
        """长度分布与延迟直方图同款累计语义：第 i 位 = ≤ bounds[i] 的请求数，
        末位 = 有值的总数（+Inf 桶）。"""
        rows = [
            _log_row(cur_side=True, finish_known=4, prompt=[100, 600, 3000, 40000], completion=[50, 200, 300, 9000])
        ]
        stats = await svc.load_log_window_stats(_db_returning(rows), NOW, NOW, NOW, NOW)
        prompt = stats.prompt_len
        assert prompt["buckets"] == [1, 1, 2, 2, 3, 3, 3, 3, 4]
        assert prompt["bounds"] == [*svc.LEN_BUCKET_BOUNDS, None]
        assert prompt["avg_tokens"] == round((100 + 600 + 3000 + 40000) / 4)
        assert stats.generation_len["buckets"] == [1, 3, 3, 3, 3, 3, 4, 4, 4]

    async def test_lengths_take_current_window_only(self):
        """分布不做环比：对照窗的行只影响截断率/吐字速度，不进分布。"""
        rows = [
            _log_row(cur_side=True, finish_known=2, prompt=[100, 200]),
            _log_row(cur_side=False, finish_known=9, prompt=[9999] * 9),
        ]
        stats = await svc.load_log_window_stats(_db_returning(rows), NOW, NOW, NOW, NOW)
        assert stats.prompt_len["buckets"][-1] == 2

    async def test_no_samples_gives_none_not_zero_histogram(self):
        """窗口内没有带 token 的行 → None（渲染空态），不是一格全 0 的直方图。"""
        rows = [_log_row(cur_side=True, finish_known=3)]
        stats = await svc.load_log_window_stats(_db_returning(rows), NOW, NOW, NOW, NOW)
        assert stats.prompt_len is None
        assert stats.generation_len is None

    async def test_tps_divides_by_decode_time(self):
        """吐字速度 = 输出 token / 生成耗时（分母只含有 ttft 的流式行）。"""
        rows = [_log_row(cur_side=True, finish_known=3, gen_tokens=900, decode_ms=30000)]
        stats = await svc.load_log_window_stats(_db_returning(rows), NOW, NOW, NOW, NOW)
        assert (stats.tps_cur, stats.tps_prev) == (30.0, None)

    async def test_tps_none_when_no_stream_or_degenerate_decode(self):
        """无流式样本、或生成耗时 ≤0（时钟粒度退化）→ null，不给假低值也不除零。"""
        rows = [_log_row(cur_side=True, finish_known=3)]
        stats = await svc.load_log_window_stats(_db_returning(rows), NOW, NOW, NOW, NOW)
        assert (stats.tps_cur, stats.tps_prev) == (None, None)

        rows = [_log_row(cur_side=True, finish_known=3, gen_tokens=100, decode_ms=0)]
        stats = await svc.load_log_window_stats(_db_returning(rows), NOW, NOW, NOW, NOW)
        assert stats.tps_cur is None

    async def test_empty_window_returns_nones(self):
        """窗口内没有落账行 → 各字段 null（渲染 -- 而非 0）。"""
        stats = await svc.load_log_window_stats(
            _StubDb(), NOW - timedelta(hours=2), NOW - timedelta(hours=1), NOW - timedelta(hours=1), NOW
        )
        assert stats == svc.LogWindowStats(
            trunc_cur=None, trunc_prev=None, tps_cur=None, tps_prev=None, prompt_len=None, generation_len=None
        )


class TestEngineHistory:
    """KV 热力图：区间 → 单元格宽度 → 聚合序列。"""

    async def test_invalid_range_raises(self):
        """反序区间必须落进 422，而不是静默当成空数据。"""
        with pytest.raises(LLMPodValidationError, match="区间非法"):
            await svc.engine_history(_StubDb(), from_=NOW, to=NOW - timedelta(hours=1))

    @pytest.mark.parametrize(
        "span,granularity",
        [(timedelta(hours=1), "5m"), (timedelta(hours=24), "1h"), (timedelta(days=7), "1d")],
    )
    async def test_cell_width_follows_span(self, span, granularity):
        """单元格宽度按跨度选档：7d 若仍按 5min 会铺出 2016 列。"""
        data = await svc.engine_history(_StubDb(), from_=NOW - span, to=NOW)
        assert data["window"]["from"] == (NOW - span).isoformat()
        assert data["window"]["to"] == NOW.isoformat()
        assert data["window"]["granularity"] == granularity

    async def test_defaults_to_today(self, monkeypatch):
        """不传区间 = 今日：起点是本地零点，跨度可能不足 6h 因而仍是 5min 格。"""
        monkeypatch.setattr(svc, "now_local", lambda: NOW)
        data = await svc.engine_history(_StubDb())
        assert data["window"]["from"] == NOW.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        assert data["window"]["to"] == NOW.isoformat()

    async def test_empty_instances_short_circuits(self):
        """无活跃实例：只查实例清单即返回，不碰 rollup（第二个 execute 不该发生）。"""
        calls = []

        class _Db:
            async def execute(self, _stmt, _params=None):
                calls.append(1)
                return _StubResult([])

        data = await svc.engine_history(_Db(), from_=NOW - timedelta(hours=1), to=NOW)
        assert data["instances"] == []
        assert len(calls) == 1

    async def test_instances_and_series_mapping(self):
        """5m 档：聚合往返与旧直读逐位等价（每格只有一行桶行）。

        旧实现直接读 bucket_start/kv_cache_util_avg 等列；聚合后
        ``SUM(avg×samples)/SUM(samples)`` 必须还原成同一个 4 位小数。
        """
        inst_rows = [("i-1", "dep-1", 0, "RUNNING", "http://e", "qwen", "llamacpp")]
        # 桶序号按服务端本地边界的口径算（与 bucket_floor_expr 同式），否则断言
        # 测的是「测试自己臆想的编号规则」而不是被测的重建逻辑
        tz_off = local_offset_seconds()
        agg_rows = [
            SimpleNamespace(
                instance_id="i-1",
                bucket_index=(int(NOW.timestamp()) + tz_off) // 300,
                samples=12,
                preemptions=2,
                kv_max=0.83,
                kv_avg_sum=0.71234 * 12,
                kv_avg_weight=12,
            ),
        ]
        data = await svc.engine_history(_db_returning(inst_rows, agg_rows), from_=NOW - timedelta(hours=1), to=NOW)
        assert data["window"]["granularity"] == "5m"
        assert len(data["instances"]) == 1
        item = data["instances"][0]
        assert item["deployment_name"] == "qwen"
        assert item["series"][0]["kv_avg"] == 0.7123  # 4 位舍入
        assert item["series"][0]["preemptions"] == 2
        assert item["series"][0]["samples"] == 12
        # 桶序号 → 起点：5min 网格对齐（NOW=14:32:07 落在 14:30 那格）
        assert item["series"][0]["ts"] == "2026-09-11T14:30:00+00:00"

    async def test_coarse_cell_merges_by_weight(self):
        """粗格合并：均值按抓取次数加权、峰值取最大、samples/preemptions 求和。"""
        inst_rows = [("i-1", "dep-1", 0, "RUNNING", "http://e", "qwen", "vllm")]
        tz_off = local_offset_seconds()
        cell_index = (int(NOW.timestamp()) + tz_off) // 3600
        agg_rows = [
            SimpleNamespace(
                instance_id="i-1",
                bucket_index=cell_index,
                samples=16,
                preemptions=3,
                kv_max=0.95,
                # 同一 1h 格里的两个 5min 桶：0.5×12 与 0.9×4 → 9.6/16 = 0.6
                kv_avg_sum=0.5 * 12 + 0.9 * 4,
                kv_avg_weight=16,
            ),
        ]
        data = await svc.engine_history(_db_returning(inst_rows, agg_rows), from_=NOW - timedelta(hours=24), to=NOW)
        point = data["instances"][0]["series"][0]
        assert data["window"]["granularity"] == "1h"
        assert point["kv_avg"] == 0.6  # 加权，不是 (0.5+0.9)/2 = 0.7
        assert point["kv_max"] == 0.95
        assert point["samples"] == 16
        assert point["preemptions"] == 3
        assert point["ts"] == datetime.fromtimestamp(cell_index * 3600 - tz_off, tz=timezone.utc).isoformat()

    async def test_kv_avg_null_when_no_gauge(self):
        """整格采到样本但引擎不暴露 KV gauge → null，不是 0。"""
        inst_rows = [("i-1", "dep-1", 0, "RUNNING", "http://e", "qwen", "llamacpp")]
        agg_rows = [
            SimpleNamespace(
                instance_id="i-1",
                bucket_index=int(NOW.timestamp()) // 300,
                samples=12,
                preemptions=0,
                kv_max=None,
                kv_avg_sum=None,
                kv_avg_weight=0,
            ),
        ]
        data = await svc.engine_history(_db_returning(inst_rows, agg_rows), from_=NOW - timedelta(hours=1), to=NOW)
        point = data["instances"][0]["series"][0]
        assert point["kv_avg"] is None
        assert point["kv_max"] is None
        assert point["samples"] == 12

    async def test_gap_cells_not_filled(self):
        """整格没有桶行 → 不产生数据点（「没抓到」不等于「水位 0」）。"""
        inst_rows = [("i-1", "dep-1", 0, "RUNNING", "http://e", "qwen", "vllm")]
        base = int(NOW.timestamp()) // 300
        agg_rows = [
            SimpleNamespace(
                instance_id="i-1",
                bucket_index=base - 2,
                samples=12,
                preemptions=0,
                kv_max=0.5,
                kv_avg_sum=6.0,
                kv_avg_weight=12,
            ),
            # base - 1 整格没抓到 → 没有分组行
            SimpleNamespace(
                instance_id="i-1",
                bucket_index=base,
                samples=12,
                preemptions=0,
                kv_max=0.6,
                kv_avg_sum=7.2,
                kv_avg_weight=12,
            ),
        ]
        data = await svc.engine_history(_db_returning(inst_rows, agg_rows), from_=NOW - timedelta(hours=1), to=NOW)
        series = data["instances"][0]["series"]
        # 缺的那格不产生点：长度就是返回行数（补 0 会先把长度撑大）
        assert len(series) == 2


class TestEngineBucketWidths:
    """热力图格宽 = 同一条跨度梯子：两个地方各写一套就会出现
    「趋势按小时、热力图按 5 分钟」这种同屏不同口径。"""

    @pytest.mark.parametrize(
        "span,seconds",
        [
            (timedelta(hours=1), 300),
            (timedelta(hours=6), 300),
            (timedelta(hours=24), 3600),
            (timedelta(hours=48), 3600),
            (timedelta(days=7), 86400),
            (timedelta(days=90), 86400),
        ],
    )
    def test_matches_the_dashboard_expectation(self, span, seconds):
        assert bucket_seconds_for(NOW - span, NOW, hourly_span_limit_seconds=_DASHBOARD_HOURLY_LIMIT) == seconds

    def test_widths_divide_the_storage_grid(self):
        """落库网格是 5min，格宽取它的整数倍才能整除桶边界。"""
        for span in (timedelta(hours=1), timedelta(hours=24), timedelta(days=30)):
            assert bucket_seconds_for(NOW - span, NOW, hourly_span_limit_seconds=_DASHBOARD_HOURLY_LIMIT) % 300 == 0

    @pytest.mark.parametrize(
        "span",
        [timedelta(hours=1), timedelta(hours=48), timedelta(days=7), timedelta(days=30), timedelta(days=90)],
    )
    def test_columns_bounded(self, span):
        """每实例列数守在趋势点数上限以内——两者共用一条梯子，热力图不该更宽。"""
        assert (
            span.total_seconds()
            // bucket_seconds_for(NOW - span, NOW, hourly_span_limit_seconds=_DASHBOARD_HOURLY_LIMIT)
            <= _MAX_TREND_POINTS
        )

    def test_short_spans_keep_five_minute_cells(self):
        """短区间必须留在 5min 格上——KV 抢占就发生在分钟级，压粗了面板就废了。"""
        assert (
            bucket_seconds_for(NOW - timedelta(hours=1), NOW, hourly_span_limit_seconds=_DASHBOARD_HOURLY_LIMIT) == 300
        )
