"""metric_query_service 纯函数单元测试（分位数插值 / 粒度选择 / 桶合并 / 日窗归一）。

SQL 聚合本体依赖 PG（FakeSession 不支持聚合查询），由本地端到端手验覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
from types import SimpleNamespace

import pytest

from tests.unit._fakes import rollup_row

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.services.dashboard_realtime_service import _DASHBOARD_HOUR_MAX_SPAN_SECONDS
from llmpod.services.metric_query_service import (
    DASHBOARD_DIMS,
    DASHBOARD_MAX_SPAN_DAYS,
    GRANULARITY_MINUTES,
    LATENCY_BUCKET_BOUNDS,
    bucket_floor_expr,
    bucket_seconds_for,
    merge_bucket_rows,
    percentile_from_buckets,
    pick_granularity,
    resolve_compare_range,
    resolve_dashboard_range,
    resolve_day_window,
    _DEPLOYMENT_HOUR_MAX_SPAN_SECONDS,
    summarize,
    token_rows_from_agg,
)


class TestPercentile:
    def test_empty_returns_none(self):
        assert percentile_from_buckets([], LATENCY_BUCKET_BOUNDS, 0.5) is None
        assert percentile_from_buckets([0] * 13, LATENCY_BUCKET_BOUNDS, 0.5) is None

    def test_all_in_first_bucket(self):
        # 10 个样本全在 le=10 桶（0-10 区间）→ P50/P95 都在 (0,10] 内
        buckets = [10] + [0] * 12
        buckets[-1] = 10
        assert 0 < percentile_from_buckets(buckets, LATENCY_BUCKET_BOUNDS, 0.5) <= 10
        assert 0 < percentile_from_buckets(buckets, LATENCY_BUCKET_BOUNDS, 0.95) <= 10

    def test_interpolates_across_buckets(self):
        # cumulative：le=50 桶 25、le=100 桶 50、le=250 桶 100（总 100 样本）
        buckets = [0, 0, 25, 50, 100] + [0] * 8
        buckets[-1] = 100
        # P50 = 第 50 个样本：恰好是 le=100 桶的边界值
        p50 = percentile_from_buckets(buckets, LATENCY_BUCKET_BOUNDS, 0.50)
        assert p50 == 100
        # P95 = 第 95 个样本：在 (100,250] 桶内插值
        p95 = percentile_from_buckets(buckets, LATENCY_BUCKET_BOUNDS, 0.95)
        assert 100 < p95 <= 250

    def test_p99_at_tail_bucket(self):
        buckets = [0] * 12 + [1000]
        p99 = percentile_from_buckets(buckets, LATENCY_BUCKET_BOUNDS, 0.99)
        assert p99 == LATENCY_BUCKET_BOUNDS[-1]


class TestGranularity:
    """一套算术，界线由调用方给 —— 函数不判断「你是谁」。

    小时/日的分界只有一个来源：调用方带进来的 ``hourly_span_limit_seconds``。
    下面用两个真实调用方的期望值跑同一组跨度，锁定「期望不同、实现只有一份」。
    """

    TO = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)
    # 直接引用两个调用方自己的期望值：改了那边的常量，这里跟着红——测试里重抄
    # 一遍数值的话，改了大屏的界线这条仍会绿，就不再代表任何真实调用方了
    DASHBOARD = _DASHBOARD_HOUR_MAX_SPAN_SECONDS
    DEPLOYMENT = _DEPLOYMENT_HOUR_MAX_SPAN_SECONDS

    def _pick(self, span: timedelta, limit: int) -> str:
        return pick_granularity(self.TO - span, self.TO, hourly_span_limit_seconds=limit)

    def test_five_minute_boundary(self):
        """6h 本身算 5m 档，多一秒就跳 1h——边界写反会让最长的一档 5m 消失。"""
        assert self._pick(timedelta(hours=6), self.DASHBOARD) == "5m"
        assert self._pick(timedelta(hours=6, seconds=1), self.DASHBOARD) == "1h"

    @pytest.mark.parametrize(
        "span,limit,expected",
        [
            (timedelta(hours=7), _DASHBOARD_HOUR_MAX_SPAN_SECONDS, "1h"),
            (timedelta(hours=48), _DASHBOARD_HOUR_MAX_SPAN_SECONDS, "1h"),  # 界线本身含在内
            (timedelta(hours=48, seconds=1), _DASHBOARD_HOUR_MAX_SPAN_SECONDS, "1d"),
            (timedelta(days=3), _DASHBOARD_HOUR_MAX_SPAN_SECONDS, "1d"),  # 大屏：跨日就看日
            (timedelta(days=3), _DEPLOYMENT_HOUR_MAX_SPAN_SECONDS, "1h"),  # 部署曲线：3 天要给 72 个小时点
            (timedelta(days=7), _DEPLOYMENT_HOUR_MAX_SPAN_SECONDS, "1h"),
            (timedelta(days=8), _DEPLOYMENT_HOUR_MAX_SPAN_SECONDS, "1d"),
        ],
    )
    def test_hour_day_boundary_follows_the_caller(self, span, limit, expected):
        assert self._pick(span, limit) == expected

    def test_same_span_differs_only_by_the_caller_expectation(self):
        """同一条跨度在两种期望下结果不同，而实现只有一份——这正是拆成参数的理由。"""
        span = timedelta(days=3)
        assert self._pick(span, self.DASHBOARD) == "1d"
        assert self._pick(span, self.DEPLOYMENT) == "1h"

    def test_bucket_seconds_shares_the_same_arithmetic(self):
        """热力图格宽与槽宽必须同源，否则同屏两条曲线刻度对不上。"""
        assert (
            bucket_seconds_for(self.TO - timedelta(hours=1), self.TO, hourly_span_limit_seconds=self.DASHBOARD) == 300
        )
        assert (
            bucket_seconds_for(self.TO - timedelta(hours=24), self.TO, hourly_span_limit_seconds=self.DASHBOARD) == 3600
        )
        assert (
            bucket_seconds_for(self.TO - timedelta(days=30), self.TO, hourly_span_limit_seconds=self.DASHBOARD) == 86400
        )


class TestResolveDashboardRange:
    """区间归一：缺省今日零点 → 当前；超当前的 to 钳位；反序/超跨度抛错。"""

    NOW = datetime(2026, 9, 11, 14, 32, 7, tzinfo=timezone.utc)

    def test_defaults_to_today_start(self):
        from_, to = resolve_dashboard_range(None, None, self.NOW)
        assert from_ == self.NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        assert to == self.NOW

    def test_to_in_the_future_is_clamped(self):
        from_ = self.NOW - timedelta(hours=1)
        _, to = resolve_dashboard_range(from_, self.NOW + timedelta(hours=5), self.NOW)
        assert to == self.NOW

    def test_naive_timestamps_are_read_as_local(self):
        """naive 入参必须归一成 aware，否则与 now_local() 比较直接 TypeError。"""
        from_, to = resolve_dashboard_range(datetime(2026, 9, 11, 10, 0), None, self.NOW)
        assert from_.tzinfo is not None
        assert to.tzinfo is not None

    def test_reversed_range_raises(self):
        with pytest.raises(LLMPodValidationError, match="区间非法"):
            resolve_dashboard_range(self.NOW, self.NOW - timedelta(hours=1), self.NOW)

    def test_zero_span_raises(self):
        with pytest.raises(LLMPodValidationError, match="区间非法"):
            resolve_dashboard_range(self.NOW, self.NOW, self.NOW)

    def test_span_over_cap_raises(self):
        with pytest.raises(LLMPodValidationError, match="跨度不得超过"):
            resolve_dashboard_range(self.NOW - timedelta(days=DASHBOARD_MAX_SPAN_DAYS + 1), self.NOW, self.NOW)

    def test_span_at_cap_is_accepted(self):
        from_, _ = resolve_dashboard_range(self.NOW - timedelta(days=DASHBOARD_MAX_SPAN_DAYS), self.NOW, self.NOW)
        assert from_ < self.NOW

    def test_just_over_cap_by_hours_is_rejected(self):
        """90 天 + 23 小时也必须拒——`timedelta.days` 向下取整会把它放行。"""
        with pytest.raises(LLMPodValidationError, match="跨度不得超过"):
            resolve_dashboard_range(self.NOW - timedelta(days=DASHBOARD_MAX_SPAN_DAYS, hours=23), self.NOW, self.NOW)

    def test_to_in_next_day_still_defaults_a_valid_from(self):
        """只给 to 且 to 落在下一自然日 → 缺省起点按**钳位后**的 to 推，不该判成反序。

        回归：先推缺省再钳位会得到「起点 = 明天零点 > 终点 = 现在」，把
        「今天一整天」这种合法请求（前端 23:59 按本地钟算出次日零点）判成 422。
        """
        from_, to = resolve_dashboard_range(None, self.NOW + timedelta(hours=5), self.NOW)
        assert to == self.NOW
        assert from_ == self.NOW.replace(hour=0, minute=0, second=0, microsecond=0)


class TestResolveCompareRange:
    """对照窗校验：同给同缺、等长、紧邻。

    对照窗由调用方给，但会直接当 SQL 左界用——两个窗是当成**一段连续区间**扫的，
    所以「紧邻」是成本约束而不是产品规则。
    """

    NOW = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)
    FROM = datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc)  # 今日 00:00
    TO = NOW

    def _resolve(self, prev_from, prev_to, from_=None, to=None):
        return resolve_compare_range(prev_from, prev_to, from_ or self.FROM, to or self.TO)

    def test_both_absent_means_no_comparison(self):
        assert self._resolve(None, None) == (None, None)

    def test_half_given_raises(self):
        with pytest.raises(LLMPodValidationError, match="必须同时给出"):
            self._resolve(self.FROM - timedelta(days=1), None)

    def test_reversed_raises(self):
        with pytest.raises(LLMPodValidationError, match="必须早于终点"):
            self._resolve(self.FROM - timedelta(hours=1), self.FROM - timedelta(hours=2))

    def test_ending_after_current_raises(self):
        with pytest.raises(LLMPodValidationError, match="不得晚于当前区间终点"):
            self._resolve(self.FROM - timedelta(hours=1), self.TO + timedelta(minutes=1))

    def test_unequal_length_raises(self):
        """计数型 delta 不做跨度折算：24h 对照 1h 会报出 24 倍级偏差且看不出异常。"""
        with pytest.raises(LLMPodValidationError, match="必须等长"):
            self._resolve(self.FROM - timedelta(hours=25), self.FROM - timedelta(hours=1))

    def test_gap_over_a_day_raises(self):
        """间隔上限一天：两个窗是一段连续区间扫的，放开间隔等于让这段无限长。

        用**等长**的对照窗才测得到这条——不等长会先撞上等长检查。
        """
        span = self.TO - self.FROM
        with pytest.raises(LLMPodValidationError, match="不得超过 24 小时"):
            self._resolve(self.FROM - timedelta(days=9), self.FROM - timedelta(days=9) + span)

    def test_yesterday_same_period_is_accepted(self):
        """最松的合法预设：今日 00:00→14:00 对照昨日 00:00→14:00，间隔 10 小时。"""
        span = self.TO - self.FROM
        prev = self._resolve(self.FROM - timedelta(days=1), self.TO - timedelta(days=1))
        assert prev == (self.FROM - timedelta(days=1), self.TO - timedelta(days=1))
        assert prev[1] - prev[0] == span

    def test_adjacent_previous_window_is_accepted(self):
        """滚动窗的对照 = 紧邻上一窗（间隔 0）。"""
        from_, to = self.NOW - timedelta(hours=1), self.NOW
        assert self._resolve(from_ - timedelta(hours=1), from_, from_, to) == (
            from_ - timedelta(hours=1),
            from_,
        )

    def test_to_shift_keeps_windows_equal_length(self):
        """客户端钟快、to 被钳回服务端 now 时，钳位量按同量扣到对照窗右端。

        调用方给的四个时间戳本身完全等长（前端按同一个 ``new Date()`` 派生），
        不等长纯粹是钳位只压短当前窗造成的——所以它不该报成调用方的参数错。
        """
        server_now = self.NOW
        client_now = server_now + timedelta(seconds=3)  # 客户端钟快 3 秒
        from_, to = client_now - timedelta(hours=1), server_now  # 钳位后的当前窗
        prev_from = from_ - timedelta(hours=1)
        prev_to = client_now - timedelta(hours=1)  # 调用方给的对照窗（未受钳位影响）

        with pytest.raises(LLMPodValidationError, match="必须等长"):
            resolve_compare_range(prev_from, prev_to, from_, to)

        shifted_from, shifted_to = resolve_compare_range(prev_from, prev_to, from_, to, to_shift_seconds=3.0)
        assert shifted_to == prev_to - timedelta(seconds=3)
        # 左端不动：今日窗的「昨日同期」锚在昨日零点上，推开左端就不再是同期
        assert shifted_from == prev_from
        assert shifted_to - shifted_from == to - from_


class TestBucketFloorExpr:
    """桶起点表达式：日桶必须落在**本地**零点，不是 UTC 零点。"""

    # +08:00：5m/1h 桶加不加偏移都落在同一处（28800 是 300 与 3600 的整数倍），
    # 86400 不是——这正是日桶需要偏移的原因
    TZ = timezone(timedelta(hours=8))

    def test_day_bucket_starts_at_local_midnight(self):
        from sqlalchemy import select

        from llmpod.models import MetricEngineRollup

        expr = bucket_floor_expr(MetricEngineRollup.bucket_start, 86400, int(self.TZ.utcoffset(None).total_seconds()))
        sql = str(select(expr).compile(compile_kwargs={"literal_binds": True}))
        # 表达式里必须带时区偏移项；不带就是 UTC 零点（本地早上八点）
        assert "28800" in sql

    def test_width_lands_in_the_expression(self):
        """桶宽必须原样进表达式——宽窄写错整张热力图的列就全错。"""
        from sqlalchemy import select

        from llmpod.models import MetricEngineRollup

        def compiled(width: int) -> str:
            return str(
                select(bucket_floor_expr(MetricEngineRollup.bucket_start, width, 0)).compile(
                    compile_kwargs={"literal_binds": True}
                )
            )

        assert "floor" in compiled(3600).lower()
        assert "3600" in compiled(3600)
        assert "86400" in compiled(86400)


class TestResolveDayWindow:
    """日窗归一：naive 入参必须归为 aware，否则调用方 min/max 会抛 TypeError。"""

    def test_defaults_today_start_to_now(self, monkeypatch):
        now = datetime(2026, 9, 5, 15, 30, tzinfo=timezone.utc)
        monkeypatch.setattr("llmpod.services.metric_query_service.now_local", lambda: now)
        from_, to = resolve_day_window(None, None)
        assert (from_, to) == (datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc), now)

    def test_naive_inputs_normalized_to_aware(self):
        """无时区后缀的入参（FastAPI 对无偏移 ISO 串的解析产物）不得原样透传：
        naive 与 now_local() 的 aware 值在调用方 min/max 里比较会抛 TypeError。"""
        from_, to = resolve_day_window(
            datetime(2026, 9, 5, 0, 0),
            datetime(2026, 9, 5, 12, 0),
        )
        assert from_.tzinfo is not None and to.tzinfo is not None
        # 归一后语义不变：窗口端点仍是传入的钟面时刻
        assert (from_.hour, to.hour) == (0, 12)

    def test_explicit_aware_inputs_pass_through(self):
        from_ = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
        to = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        assert resolve_day_window(from_, to) == (from_, to)


class TestMergeAndSummarize:
    def test_merge_sums_and_classifies_status(self):
        rows = [
            rollup_row(
                requests=10,
                status="success",
                total_tokens=1000,
                latency_sum_ms=5000,
                latency_count=10,
                in_flight_peak=3,
            ),
            rollup_row(
                requests=2, status="error", total_tokens=100, latency_sum_ms=8000, latency_count=2, in_flight_peak=7
            ),
            rollup_row(requests=1, status="rejected"),
        ]
        merged = merge_bucket_rows(rows)
        assert merged["requests"] == 13
        assert merged["errors"] == 2
        assert merged["rejected"] == 1
        assert merged["total_tokens"] == 1100
        assert merged["latency_count"] == 12
        assert merged["latency_sum_ms"] == 13000
        assert merged["in_flight_peak"] == 7  # 峰值取 max

    def test_merge_buckets_elementwise(self):
        b1 = rollup_row(latency_buckets=[5] + [0] * 12)
        b1.latency_buckets[-1] = 5
        b2 = rollup_row(latency_buckets=[3] + [0] * 12)
        b2.latency_buckets[-1] = 3
        merged = merge_bucket_rows([b1, b2])
        assert merged["latency_buckets"][0] == 8
        assert merged["latency_buckets"][-1] == 8

    def test_summarize_derives_rates_and_quantiles(self):
        row = rollup_row(requests=100, status="success", total_tokens=5000, latency_sum_ms=100000, latency_count=100)
        row.latency_buckets[2] = 100  # 全部 ≤50ms
        row.latency_buckets[-1] = 100
        s = summarize(merge_bucket_rows([row]))
        assert s["requests"] == 100
        assert s["latency_avg_ms"] == 1000
        # P50 = 第 50 个样本，落在 (25,50] 桶内插值
        assert 25 < s["latency_p50_ms"] <= 50
        assert s["error_rate"] == 0.0
        assert s["ttft_avg_ms"] is None  # 无 TTFT 样本


def _agg_row(dim_key, **kw) -> SimpleNamespace:
    """SQL 聚合行替身（dim_key + 四项 token 和，SQL GROUP BY 的产物形态）。"""
    return SimpleNamespace(dim_key=dim_key, **kw)


class TestTokenRows:
    """Token 汇总聚合：口径（cached ⊂ input、total = input+output）与折叠。"""

    def test_model_dim_ranking_and_ratios(self):
        rows = [
            _agg_row("m1", prompt_tokens=600, completion_tokens=200, cached_tokens=300, total_tokens=800),
            _agg_row("m2", prompt_tokens=100, completion_tokens=50, cached_tokens=10, total_tokens=150),
            _agg_row(None, prompt_tokens=999, total_tokens=999),  # 无维度身份，不进汇总
        ]
        out = token_rows_from_agg(rows, top=5)
        assert [r["key"] for r in out["rows"]] == ["m1", "m2"]
        m1 = out["rows"][0]
        assert m1["input_tokens"] == 600
        assert m1["total_tokens"] == 800
        assert m1["ratio"] == round(800 / 950, 4)
        assert out["other"] is None  # 未超 top 不折叠
        # 口径：total = input + output；cached ≤ input（「其中缓存」不进合计算式）
        assert out["total"]["total_tokens"] == out["total"]["input_tokens"] + out["total"]["output_tokens"]
        assert out["total"]["cached_tokens"] <= out["total"]["input_tokens"]

    def test_top_folding_and_ratio_sum(self):
        rows = [
            _agg_row(
                f"m{i}",
                prompt_tokens=10 * (8 - i),
                completion_tokens=0,
                cached_tokens=0,
                total_tokens=10 * (8 - i),
            )
            for i in range(7)
        ]
        out = token_rows_from_agg(rows, top=2)
        assert [r["key"] for r in out["rows"]] == ["m0", "m1"]
        assert out["other"]["key"] == "__other__"
        assert out["other"]["count"] == 5
        assert out["other"]["total_tokens"] == 60 + 50 + 40 + 30 + 20
        # rows + other 的 ratio 之和为 1
        total_ratio = sum(r["ratio"] for r in out["rows"]) + out["other"]["ratio"]
        assert abs(total_ratio - 1.0) < 0.01

    def test_names_applied_and_unknown_falls_back(self):
        rows = [_agg_row("m1", prompt_tokens=0, completion_tokens=0, cached_tokens=0, total_tokens=5)]
        out = token_rows_from_agg(rows, top=5, names={"m1": "显示名"})
        assert out["rows"][0]["key"] == "显示名"
        out2 = token_rows_from_agg(rows, top=5, names={})
        assert out2["rows"][0]["key"] == "m1"

    def test_empty_rows_zero_shape(self):
        out = token_rows_from_agg([], top=5)
        assert out["rows"] == []
        assert out["other"] is None
        assert out["total"]["total_tokens"] == 0

    def test_dashboard_dims_single_source(self):
        """/realtime 与 /tokens 的 dim 词表单源对账：路由 Literal 由它派生，
        两端点对非法值的行为必须一致（422）。"""
        import typing

        from llmpod.apps.webserver.routes.admin.dashboard import DimParam

        assert set(typing.get_args(DimParam)) == set(DASHBOARD_DIMS)

    def test_dashboard_endpoints_take_a_range(self):
        """三个大屏端点都收 from/to —— 窗口词表分叉过的那个坑不许再回来。

        一次性把旧参数拒掉（FastAPI 对未声明参数是静默丢的），这条只测拒绝本身；
        「老前端发 window 会拿到 422」的端到端效应由集成测试覆盖。
        """
        from llmpod.apps.webserver.routes.admin.dashboard import (
            _reject_removed_params,
            dashboard_anomaly,
            dashboard_engine_history,
            dashboard_realtime,
        )

        for endpoint in (dashboard_realtime, dashboard_engine_history, dashboard_anomaly):
            assert {"from_", "to"} <= set(inspect.signature(endpoint).parameters), endpoint.__name__

        with pytest.raises(LLMPodValidationError):
            _reject_removed_params("1h")
        with pytest.raises(LLMPodValidationError):
            _reject_removed_params(None, "5m")

    def test_hot_dim_labels_cover_dashboard_dims(self):
        """写侧热层维度对账：DASHBOARD_DIMS 新增维度而 scraper 的热层标签
        映射没跟上时，/realtime 热层构成会静默返回空（无报错、不回冷层）。"""
        from llmpod.apps.webserver.metrics_scraper import _HOT_DIM_LABELS

        hot_dims = {dim for dim, _label in _HOT_DIM_LABELS}
        assert set(DASHBOARD_DIMS) <= hot_dims

    def test_rollup_bucket_width_single_source(self):
        """冷层桶宽对账：写侧攒桶网格与查询侧 5m 粒度必须同宽——两处字面量
        各改一半会让冷层速率静默按比例失真（分母错、分子对）。"""
        from llmpod.apps.webserver.metrics_scraper import _BUCKET_SECONDS

        assert _BUCKET_SECONDS == GRANULARITY_MINUTES["5m"] * 60

    def test_other_key_sentinels_agree(self):
        """长尾折叠桶名对账：scraper 席位折叠与查询层 top-N 外折叠用的是同一个
        桶——任一侧改名后长尾会被当成普通序列参与 top-5 排名（且它通常最大）。"""
        from llmpod.apps.webserver.metrics_scraper import _HOT_OTHER_KEY
        from llmpod.services.dashboard_realtime_service import _OTHER_KEY as query_other

        assert _HOT_OTHER_KEY == query_other

    def test_hot_pruning_thresholds_cover_reader_window(self):
        """热层修剪阈值对账：闲置修剪不得小于读侧最大热窗（1h）——间歇流量的
        key 会在窗口内静默消失；顶替阈值必须严格更小（否则永远等不到顶替就
        被修剪）。这些关系此前只活在注释里。"""
        from llmpod.apps.webserver.metrics_scraper import (
            _DIM_RING_RETENTION_SECONDS,
            _HOT_EVICT_IDLE,
            _HOT_IDLE_SECONDS,
        )

        # 读侧走热层的最大跨度：写侧保留期必须盖得住它，否则区间还没到头
        # 热层就先丢了点，覆盖率判定会静默回落到冷层（表现为热层时有时无）
        from llmpod.services.dashboard_realtime_service import _HOT_MAX_AGE_SECONDS

        # 热层闸门按构成维度环的保留深度判（见该常量注释），两者必须同源
        assert _HOT_MAX_AGE_SECONDS == _DIM_RING_RETENTION_SECONDS
        assert _HOT_IDLE_SECONDS >= _HOT_MAX_AGE_SECONDS
        assert _HOT_EVICT_IDLE < _HOT_IDLE_SECONDS
        assert _DIM_RING_RETENTION_SECONDS >= _HOT_MAX_AGE_SECONDS
