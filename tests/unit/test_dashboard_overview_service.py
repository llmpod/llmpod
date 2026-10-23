"""dashboard_overview_service / dashboard_anomaly_service 单元测试。

纯函数部分（双窗 KPI 量纲 / 小时双线并排 / 错误分类表）直接构造输入验证；
SQL 取行与整屏组装依赖 PG，在真库环境另验（integration 侧只测路由契约）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.services import dashboard_anomaly_service as anom
from llmpod.services import dashboard_overview_service as ov

TZ = timezone.utc
TODAY0 = datetime(2026, 9, 15, 0, 0, tzinfo=TZ)  # 今日零点
YDAY0 = TODAY0 - timedelta(days=1)


def _bucket_row(bucket_start: datetime, **kw) -> SimpleNamespace:
    """load_bucket_rows 聚合行替身（按 bucket_start 分组，窗侧在 Python 侧推导）。"""
    base = dict(calls=0, errors=0, forwarded=0, ttft_sum_ms=0, ttft_count=0)
    base.update(kw)
    return SimpleNamespace(bucket_start=bucket_start, **base)


class TestSplitDualRows:
    def test_yesterday_cut_off_at_same_time(self):
        """KPI 对照窗截至昨日同时刻：晚于它的昨日行剔除（今日 11 点时昨 20 点不算）。"""
        rows = [
            _bucket_row(TODAY0 + timedelta(hours=11), calls=100),  # 今日 11 点
            _bucket_row(YDAY0 + timedelta(hours=10), calls=80),  # 昨日 10 点（同期内，保留）
            _bucket_row(YDAY0 + timedelta(hours=20), calls=999),  # 昨日 20 点（今日未走到，剔除）
        ]
        today_rows, yesterday_rows = ov.split_dual_rows(rows, TODAY0, YDAY0 + timedelta(hours=11))
        assert [r.calls for r in today_rows] == [100]
        assert [r.calls for r in yesterday_rows] == [80]

    def test_mid_hour_boundary_kept(self):
        """偏移恰在界上的昨日行保留（同小时槽的部分数据也算同期）。"""
        rows = [
            _bucket_row(YDAY0 + timedelta(hours=11), calls=5),
            _bucket_row(YDAY0 + timedelta(hours=11, minutes=10), calls=6),
        ]
        _, yesterday_rows = ov.split_dual_rows(rows, TODAY0, YDAY0 + timedelta(hours=11, minutes=5))
        assert [r.calls for r in yesterday_rows] == [5]


class TestHourlyDualTrend:
    def test_hours_aligned_by_wall_clock(self):
        """今日 10/11 点与昨日 10/11 点落同槽（各以自身零点计偏移）。"""
        today = [
            _bucket_row(TODAY0 + timedelta(hours=10), calls=100),
            _bucket_row(TODAY0 + timedelta(hours=11), calls=50),
        ]
        yesterday = [
            _bucket_row(YDAY0 + timedelta(hours=10), calls=80),
            _bucket_row(YDAY0 + timedelta(hours=11), calls=60),
        ]
        trend = ov.hourly_dual_trend(today, yesterday, TODAY0)
        assert [p["hour"] for p in trend["points"]] == [10, 11]
        assert trend["points"][0] == {"hour": 10, "calls": 100, "calls_yesterday": 80}
        assert trend["points"][1] == {"hour": 11, "calls": 50, "calls_yesterday": 60}
        assert trend["today_total"] == 150
        assert trend["yesterday_same_period_total"] == 140

    def test_yesterday_extra_hours_not_plotted(self):
        """昨日下午（今日还没走到的时段）不进 X 轴——对照线尾部短一截是预期。"""
        today = [_bucket_row(TODAY0, calls=10)]  # 今日 0 点
        yesterday = [
            _bucket_row(YDAY0, calls=5),
            _bucket_row(YDAY0 + timedelta(hours=6), calls=99),
        ]
        trend = ov.hourly_dual_trend(today, yesterday, TODAY0)
        assert [p["hour"] for p in trend["points"]] == [0]
        assert trend["yesterday_same_period_total"] == 5  # 昨日 6 点槽（今日未走到）不计

    def test_same_hour_multiple_buckets_accumulate(self):
        """同一小时内的多个 5min 桶行并槽累加。"""
        today = [
            _bucket_row(TODAY0 + timedelta(minutes=10), calls=1),
            _bucket_row(TODAY0 + timedelta(minutes=20), calls=2),
        ]
        trend = ov.hourly_dual_trend(today, [], TODAY0)
        assert trend["points"][0]["calls"] == 3


class TestWindowSums:
    def test_sums_each_field(self):
        rows = [
            _bucket_row(TODAY0, calls=10, errors=1, forwarded=10, ttft_sum_ms=1000, ttft_count=2),
            _bucket_row(TODAY0 + timedelta(hours=1), calls=5, errors=0, forwarded=5, ttft_sum_ms=500, ttft_count=1),
        ]
        sums = ov._window_sums(rows)
        assert sums == {
            "calls": 15,
            "errors": 1,
            "forwarded": 15,
            "ttft_sum_ms": 1500,
            "ttft_count": 3,
        }


class TestOverviewKpis:
    def test_all_delta_flavors(self):
        """三种 delta 量纲：calls=%、计数=绝对差、比率=pp、延迟=ms。"""
        today = {
            "calls": 1100,
            "forwarded": 1100,
            "errors": 11,
            "ttft_sum_ms": 610000,
            "ttft_count": 1000,
            "models": 12,
            "tenants": 38,
        }
        yesterday = {
            "calls": 1000,
            "forwarded": 1000,
            "errors": 5,
            "ttft_sum_ms": 630000,
            "ttft_count": 1000,
            "models": 11,
            "tenants": 36,
        }
        kpi = ov.overview_kpis(today, yesterday)
        assert kpi["calls"] == {"value": 1100, "delta_pct": 10.0}
        assert kpi["active_tenants"] == {"value": 38, "delta": 2}
        assert kpi["active_models"] == {"value": 12, "delta": 1}
        # success_rate: 今 (1100-11)/1100=0.99、昨 (1000-5)/1000=0.995 → -0.5pp
        assert kpi["success_rate"]["value"] == 0.99
        assert kpi["success_rate"]["delta_pp"] == -0.5
        assert kpi["avg_ttft_ms"] == {"value": 610, "delta_ms": -20}

    def test_no_yesterday_data_all_deltas_null(self):
        """昨日无数据：value 照给、delta 全 null（不猜）。"""
        today = {
            "calls": 10,
            "forwarded": 10,
            "errors": 0,
            "ttft_sum_ms": 0,
            "ttft_count": 0,
            "models": 1,
            "tenants": 1,
        }
        yesterday = {
            "calls": 0,
            "forwarded": 0,
            "errors": 0,
            "ttft_sum_ms": 0,
            "ttft_count": 0,
            "models": 0,
            "tenants": 0,
        }
        kpi = ov.overview_kpis(today, yesterday)
        assert kpi["calls"]["value"] == 10
        assert kpi["calls"]["delta_pct"] is None
        assert kpi["active_tenants"]["delta"] is None
        assert kpi["avg_ttft_ms"]["value"] is None  # 今日也无 TTFT 样本


class TestClassifyErrorMessage:
    """分类表对齐产出侧词表：upstream HTTP {status} / upstream {异常名} / stream failed: {异常名}。"""

    def test_actual_producer_vocabulary(self):
        """真实产出形态逐条对账（500/429/400 风暴不得全落 other）。"""
        cases = {
            "upstream HTTP 500": "upstream_5xx",
            "upstream HTTP 502": "upstream_5xx",
            "upstream HTTP 503": "upstream_5xx",
            "upstream HTTP 429": "rate_limited",
            "upstream HTTP 401": "upstream_4xx",
            "upstream HTTP 400": "upstream_4xx",
            "upstream ReadTimeout": "upstream_timeout",
            "upstream ConnectError": "upstream_connect",
            "stream failed: RemoteProtocolError": "other",  # 协议断流无专类，落 other
            "stream failed: ReadTimeout": "upstream_timeout",
        }
        for message, kind in cases.items():
            assert anom.classify_error_message(message) == kind, message

    def test_priority_specific_before_generic(self):
        """顺序匹配：timeout 命中在 5xx 前（同一条消息含两个关键词时取先声明者）。"""
        assert anom.classify_error_message("upstream request timed out after 30s") == "upstream_timeout"
        assert anom.classify_error_message("upstream 503 bad gateway") == "upstream_5xx"
        assert anom.classify_error_message("upstream timeout then 502") == "upstream_timeout"
        # 429 在 upstream http 4 之前（同为 4xx 前缀，先专后泛）
        assert anom.classify_error_message("upstream HTTP 429") == "rate_limited"

    def test_each_kind(self):
        cases = {
            "context_length_exceeded": "context_length",
            "request context length too long": "context_length",
            "insufficient balance": "quota_exhausted",
            "quota exceeded for this key": "quota_exhausted",
            "rate limit reached": "rate_limited",
            "401 unauthorized": "upstream_4xx",
            "connection refused by upstream": "upstream_connect",
        }
        for message, kind in cases.items():
            assert anom.classify_error_message(message) == kind, message

    def test_none_and_unknown_fall_to_other(self):
        assert anom.classify_error_message(None) == "other"
        assert anom.classify_error_message("") == "other"
        assert anom.classify_error_message("weird failure") == "other"

    def test_case_insensitive(self):
        assert anom.classify_error_message("Request TIMED OUT") == "upstream_timeout"

    def test_sql_case_and_python_agree(self):
        """SQL CASE 片段与 Python 分类对同一批消息给同 kind（两套实现的对账）。"""
        import re

        sql = anom._case_sql()
        sql_kinds = re.findall(r"THEN '(\w+)'", sql)
        py_kinds = [kind for kind, _ in anom.ERROR_KIND_MATCHERS]
        assert sql_kinds == py_kinds
        assert re.search(r"ELSE 'other'", sql) is not None


class TestAnomalyUpstreamBlock:
    def _type_row(self, upstream_type, requests, errors=0, rejected=0, lat_sum=0, lat_count=0):
        return SimpleNamespace(
            upstream_type=upstream_type,
            requests=requests,
            errors=errors,
            rejected=rejected,
            latency_sum_ms=lat_sum,
            latency_count=lat_count,
        )

    def test_cards_and_endpoint_order(self):
        type_rows = [
            self._type_row("local", 810, errors=3, lat_sum=810000, lat_count=1300),
            self._type_row("external", 190, errors=16, lat_sum=190000, lat_count=100),
        ]
        endpoint_rows = [
            SimpleNamespace(
                endpoint_name="xfyun", requests=100, errors=16, rejected=0, latency_sum_ms=260000, latency_count=100
            ),
            SimpleNamespace(
                endpoint_name="openai", requests=90, errors=0, rejected=0, latency_sum_ms=90000, latency_count=100
            ),
        ]
        block = anom._upstream_block(type_rows, endpoint_rows, minutes=60)
        assert block["local"]["qpm"] == 13.5  # 810 / 60
        assert block["local"]["success_rate"] == pytest.approx(1 - 3 / 810, abs=1e-4)
        assert block["local"]["latency_avg_ms"] == round(810000 / 1300)
        assert block["external"]["success_rate"] == pytest.approx(1 - 16 / 190, abs=1e-4)
        # 外部端点按请求量降序
        assert [p["name"] for p in block["providers"]] == ["xfyun", "openai"]
        assert block["providers"][0]["qpm"] == round(100 / 60, 1)
        assert block["providers"][0]["success_rate"] == pytest.approx(0.84, abs=1e-3)

    def test_empty_type_rows_none_cards(self):
        """无上游流量：local/external 卡为 null（渲染 --），不冒充 0。"""
        block = anom._upstream_block([], [], minutes=60)
        assert block["local"] is None
        assert block["external"] is None
        assert block["providers"] == []


NOW = datetime(2026, 9, 11, 14, 32, 7, tzinfo=timezone.utc)


class TestAnomalyWindow:
    """异常面板区间：与热力图/`/realtime` 同一套 from/to 语义（同屏一个区间控件）。

    ``anomaly()`` 的第一件事就是 resolve_dashboard_range，所以非法区间在碰 DB
    之前就抛——这里的用例可以直接传 ``None`` 当 session。跨过这一步的用例一律
    把四个查询全部替换成桩，避免造假 session。
    """

    def _patch_queries(self, monkeypatch, captured, *, type_rows=None):
        async def _fake_rejects(db, from_, to):
            captured["from"] = from_
            captured["to"] = to
            return [], 0

        async def _fake_errors(db, from_, to):
            return []

        async def _fake_types(db, from_, to):
            return type_rows or []

        monkeypatch.setattr(anom, "_reject_items", _fake_rejects)
        monkeypatch.setattr(anom, "_error_items", _fake_errors)
        monkeypatch.setattr(anom.mq, "upstream_type_rows", _fake_types)

    async def test_explicit_range_is_used_verbatim(self, monkeypatch):
        monkeypatch.setattr(anom, "now_local", lambda: NOW)
        captured: dict = {}
        self._patch_queries(monkeypatch, captured)

        from_ = NOW - timedelta(days=30)
        data = await anom.anomaly(None, from_=from_, to=NOW)

        assert captured["from"] == from_
        assert captured["to"] == NOW
        assert data["window"]["from"] == from_.isoformat()
        assert data["window"]["to"] == NOW.isoformat()

    async def test_defaults_to_today(self, monkeypatch):
        monkeypatch.setattr(anom, "now_local", lambda: NOW)
        captured: dict = {}
        self._patch_queries(monkeypatch, captured)

        await anom.anomaly(None)

        assert captured["from"] == NOW.replace(hour=0, minute=0, second=0, microsecond=0)

    async def test_qpm_denominator_is_the_range_span(self, monkeypatch):
        """QPM 分母 = 区间跨度，由两个时间戳直接相减（不再查窗口名表）。"""
        monkeypatch.setattr(anom, "now_local", lambda: NOW)
        captured: dict = {}
        local_row = SimpleNamespace(
            upstream_type="local", requests=600, errors=0, rejected=0, latency_sum_ms=0, latency_count=0
        )
        self._patch_queries(monkeypatch, captured, type_rows=[local_row])

        # 10 小时区间：600 请求 / 600 分钟 = 1.0 QPM
        data = await anom.anomaly(None, from_=NOW - timedelta(hours=10), to=NOW)

        assert data["upstream"]["local"]["qpm"] == 1.0

    @pytest.mark.parametrize("kwargs", [{"from_": NOW, "to": NOW - timedelta(hours=1)}, {"from_": NOW, "to": NOW}])
    async def test_invalid_range_raises(self, kwargs):
        with pytest.raises(LLMPodValidationError, match="区间非法"):
            await anom.anomaly(None, **kwargs)
