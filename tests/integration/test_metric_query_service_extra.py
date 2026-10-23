"""指标查询 service 聚合链路补充测试（OpSession 承载 MetricRollup/Node/Tenant 行）。

既有 tests/unit/test_metric_query_service.py 覆盖纯函数；本文件专打带 DB 读的
聚合入口与窗口/维度守卫分支：
- load_rows / summary_core（含租户收敛、无效窗口 → None）；
- summary（节点在线计数装配、无效窗口零值形状）；
- trend / trend_from_rows / trend_day_list（逐日聚合、空日补 0）；
- model_ranking / tenant_ranking（分组排序、占比、平台 Key 排除、未知模型归并）；
- dimension_series（粒度选择、qps/tps/tpm、非法维度/窗口 → []）；
- merge/percentile 边角：interrupted 分类、超长桶数组截断、宽度 0 插值；
- upstream_endpoint_rows 的取数口径（形状锁，见该类 docstring 说明为何不做行为断言）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects import postgresql

from _fakes import _FakeResult
from tests.integration._op_session import OpSession

from llmpod.config.constants import NodeStatus
from llmpod.models.metric_rollup import MetricRollup
from llmpod.models.node import Node
from llmpod.models.tenant import Tenant
from llmpod.services import metric_query_service as mqs

TZ = timezone(timedelta(hours=8))  # 固定偏移，测试环境无 tzdata 依赖
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=TZ)


def _row(
    rid: int,
    *,
    bucket: datetime,
    model: str | None = None,
    tenant_id: int | None = None,
    status: str = "success",
    requests: int = 0,
    total_tokens: int = 0,
    latency_sum: int = 0,
    latency_count: int = 0,
    ttft_sum: int = 0,
    ttft_count: int = 0,
    deployment_id: str | None = None,
    endpoint_id: int | None = None,
    latency_buckets: list[int] | None = None,
) -> MetricRollup:
    n = len(mqs.LATENCY_BUCKET_BOUNDS) + 1
    return MetricRollup(
        id=rid,
        bucket_start=bucket,
        source=f"src-{rid}",
        tenant_id=tenant_id,
        model_alias=model,
        status=status,
        requests=requests,
        total_tokens=total_tokens,
        latency_sum_ms=latency_sum,
        latency_count=latency_count,
        ttft_sum_ms=ttft_sum,
        ttft_count=ttft_count,
        deployment_id=deployment_id,
        endpoint_id=endpoint_id,
        latency_buckets=latency_buckets or [0] * n,
        ttft_buckets=[0] * n,
    )


def _session(*rows: MetricRollup, nodes: list[Node] | None = None, tenants: list[Tenant] | None = None) -> OpSession:
    s = OpSession()
    for r in rows:
        s.preload(r)
    for n in nodes or []:
        s.preload(n)
    for t in tenants or []:
        s.preload(t)
    return s


FROM = datetime(2026, 9, 1, 0, 0, tzinfo=TZ)
TO = datetime(2026, 9, 5, 0, 0, tzinfo=TZ)


class TestLoadAndCore:
    async def test_load_rows_window_and_tenant_convergence(self):
        s = _session(
            _row(1, bucket=datetime(2026, 9, 2, 10, 0, tzinfo=TZ), tenant_id=5, requests=10),
            _row(2, bucket=datetime(2026, 9, 3, 10, 0, tzinfo=TZ), tenant_id=6, requests=20),
            _row(3, bucket=datetime(2026, 8, 1, 10, 0, tzinfo=TZ), tenant_id=5, requests=40),  # 窗口外
        )
        rows = await mqs.load_rows(s, FROM, TO, tenant_id=5)
        assert [r.id for r in rows] == [1]  # 窗口 + 租户收敛

    async def test_summary_core_counts_forwarded_models_tenants(self):
        s = _session(
            _row(
                1,
                bucket=datetime(2026, 9, 2, tzinfo=TZ),
                model="m1",
                tenant_id=5,
                status="success",
                requests=10,
                total_tokens=100,
            ),
            _row(
                2,
                bucket=datetime(2026, 9, 2, tzinfo=TZ),
                model="m2",
                tenant_id=None,
                status="error",
                requests=5,
                total_tokens=50,
            ),
            _row(
                3, bucket=datetime(2026, 9, 2, tzinfo=TZ), model="m1", tenant_id=6, status="rejected", requests=100
            ),  # rejected：不计 forwarded、不计活跃
            _row(4, bucket=datetime(2026, 9, 2, tzinfo=TZ), model=None, tenant_id=6, status="interrupted", requests=1),
        )
        core = await mqs.summary_core(s, from_=FROM, to=TO)
        assert core["calls"] == 10 + 5 + 1  # rejected 不进 calls
        assert core["active_models"] == 2  # m1/m2 均有 forwarded 行
        assert core["active_tenants"] == 2  # 5/6 各有 forwarded 行
        assert core["error_rate"] == round(5 / 16, 4)  # 分母只取触达上游
        assert core["total_tokens"] == 150

    async def test_summary_core_invalid_window_returns_none(self):
        s = _session(_row(1, bucket=datetime(2026, 9, 2, tzinfo=TZ), requests=1))
        assert await mqs.summary_core(s, from_=TO, to=FROM) is None  # to <= from

    async def test_summary_assembles_node_counts(self):
        s = _session(
            _row(1, bucket=datetime(2026, 9, 2, tzinfo=TZ), requests=3),
            nodes=[
                Node(id="n1", cluster_id=1, hostname="a", ip="10.0.0.1", status=NodeStatus.ONLINE),
                Node(id="n2", cluster_id=1, hostname="b", ip="10.0.0.2", status=NodeStatus.ONLINE),
                Node(id="n3", cluster_id=1, hostname="c", ip="10.0.0.3", status="offline"),
            ],
        )
        result = await mqs.summary(s, from_=FROM, to=TO)
        assert result["online_nodes"] == 2
        assert result["total_nodes"] == 3

    async def test_summary_invalid_window_zero_shape(self):
        s = _session(
            nodes=[Node(id="n1", cluster_id=1, hostname="a", ip="10.0.0.1", status=NodeStatus.ONLINE)],
        )
        result = await mqs.summary(s, from_=TO, to=FROM)
        assert result == {**mqs.ZERO_CORE, "online_nodes": 0, "total_nodes": 0}


class TestTrend:
    def test_day_list_includes_today(self):
        days = mqs.trend_day_list(3, NOW)
        assert len(days) == 3  # 近 N 天含今天共 N 个点（与 tenants/overview 窗口同口径）
        assert days[0].isoformat() == "2026-09-03"
        assert days[-1].isoformat() == "2026-09-05"

    def test_trend_from_rows_merges_and_fills_empty_days(self):
        day_list = mqs.trend_day_list(2, NOW)
        rows = [
            _row(1, bucket=datetime(2026, 9, 4, 8, 0, tzinfo=TZ), status="success", requests=10),
            _row(2, bucket=datetime(2026, 9, 4, 9, 0, tzinfo=TZ), status="error", requests=2),
            _row(3, bucket=datetime(2026, 9, 3, 23, 0, tzinfo=TZ), status="success", requests=1),  # day_list 外
        ]
        points = mqs.trend_from_rows(rows, day_list)
        by_date = {p["date"]: p for p in points}
        assert by_date["2026-09-04"]["call_count"] == 12
        assert by_date["2026-09-04"]["error_rate"] == round(2 / 12, 4)
        assert by_date["2026-09-05"] == {"date": "2026-09-05", "call_count": 0, "error_rate": 0.0}  # 空日补 0

    async def test_trend_end_to_end_with_tenant_filter(self, monkeypatch):
        monkeypatch.setattr(mqs, "now_local", lambda: NOW)  # 冻结"今天"
        s = _session(
            _row(1, bucket=datetime(2026, 9, 4, 8, 0, tzinfo=TZ), tenant_id=5, requests=7),
            _row(2, bucket=datetime(2026, 9, 4, 9, 0, tzinfo=TZ), tenant_id=6, requests=9),
        )
        points = await mqs.trend(s, days=2, tenant_id=5)
        by_date = {p["date"]: p for p in points}
        assert by_date["2026-09-04"]["call_count"] == 7
        assert by_date["2026-09-05"]["call_count"] == 0  # 空日补 0


class TestRankings:
    async def test_model_ranking_sorts_and_computes_ratio(self):
        s = _session(
            _row(
                1,
                bucket=datetime(2026, 9, 2, tzinfo=TZ),
                model="m1",
                status="success",
                requests=60,
                ttft_sum=100,
                ttft_count=2,
            ),
            _row(2, bucket=datetime(2026, 9, 2, tzinfo=TZ), model="m2", status="success", requests=30),
            _row(3, bucket=datetime(2026, 9, 2, tzinfo=TZ), model=None, status="error", requests=10),
        )
        items = await mqs.model_ranking(s, from_=FROM, to=TO, top_n=2)
        assert [i["model"] for i in items] == ["m1", "m2"]
        assert items[0]["call_count"] == 60
        assert items[0]["ratio"] == round(60 / 100, 4)  # 分母含 (unknown) 组的 forwarded
        assert items[1]["ratio"] == round(30 / 100, 4)
        assert items[0]["avg_ttft_ms"] == 50
        assert len(items) == 2  # top_n 截断，(unknown) 组被挤出

    async def test_model_ranking_invalid_window_empty(self):
        s = _session(_row(1, bucket=datetime(2026, 9, 2, tzinfo=TZ), requests=1))
        assert await mqs.model_ranking(s, from_=TO, to=FROM) == []

    async def test_model_ranking_merges_latency_and_truncation(self, monkeypatch):
        """平均延迟取自 rollup（latency_sum/count），截断率按模型名从 request_log 回填。

        两条来源不同：延迟在桶里，截断布尔只在逐请求落账行上——所以后者是
        单独一条 GROUP BY 的结果，按模型名对号入座（对不上的为 None）。
        """

        async def _fake_truncation(_db, _from, _to, *, model=None, tenant_id=None):
            return {"m1": 0.25}

        monkeypatch.setattr(mqs, "_model_truncation_rates", _fake_truncation)
        s = _session(
            _row(
                1,
                bucket=datetime(2026, 9, 2, tzinfo=TZ),
                model="m1",
                status="success",
                requests=60,
                latency_sum=120000,
                latency_count=200,
            ),
            _row(2, bucket=datetime(2026, 9, 2, tzinfo=TZ), model="m2", status="success", requests=30),
        )
        items = await mqs.model_ranking(s, from_=FROM, to=TO, top_n=2)
        assert items[0]["avg_latency_ms"] == 600  # 120000 / 200
        assert items[0]["truncation_rate"] == 0.25
        assert items[1]["avg_latency_ms"] is None  # 无延迟样本
        assert items[1]["truncation_rate"] is None  # request_log 里没有 m2 的落账行

    async def test_model_ranking_no_logged_rows_gives_none_truncation(self, monkeypatch):
        """窗口内没有落账行（如刚上线）→ 截断率 null，而不是 0。"""

        async def _fake_truncation(_db, _from, _to, *, model=None, tenant_id=None):
            return {}

        monkeypatch.setattr(mqs, "_model_truncation_rates", _fake_truncation)
        s = _session(_row(1, bucket=datetime(2026, 9, 2, tzinfo=TZ), model="m1", requests=5))
        items = await mqs.model_ranking(s, from_=FROM, to=TO)
        assert items[0]["truncation_rate"] is None

    async def test_tenant_ranking_excludes_platform_and_resolves_names(self):
        s = _session(
            _row(1, bucket=datetime(2026, 9, 2, tzinfo=TZ), tenant_id=1, status="success", requests=30),
            _row(2, bucket=datetime(2026, 9, 2, tzinfo=TZ), tenant_id=2, status="success", requests=10),
            _row(
                3, bucket=datetime(2026, 9, 2, tzinfo=TZ), tenant_id=None, status="success", requests=999
            ),  # 平台 Key 不计
            tenants=[Tenant(id=1, name="Acme")],  # 租户 2 无名 → 兜底标签
        )
        items = await mqs.tenant_ranking(s, from_=FROM, to=TO, top_n=5)
        assert [i["tenant_id"] for i in items] == [1, 2]
        assert items[0]["tenant"] == "Acme"
        assert items[1]["tenant"] == "租户#2"
        assert items[0]["ratio"] == round(30 / 40, 4)
        assert items[1]["ratio"] == round(10 / 40, 4)


class TestDimensionSeries:
    async def test_series_with_fixed_granularity(self):
        s = _session(
            _row(
                1,
                bucket=datetime(2026, 9, 2, 10, 5, tzinfo=TZ),
                deployment_id="d-1",
                status="success",
                requests=10,
                total_tokens=300,
            ),
            _row(
                2,
                bucket=datetime(2026, 9, 2, 10, 20, tzinfo=TZ),
                deployment_id="d-1",
                status="success",
                requests=5,
                total_tokens=100,
            ),
            _row(
                3, bucket=datetime(2026, 9, 2, 10, 5, tzinfo=TZ), deployment_id="d-2", status="success", requests=999
            ),  # 其他维度成员不计
        )
        series = await mqs.dimension_series(
            s,
            dim="deployment",
            dim_id="d-1",
            from_=datetime(2026, 9, 2, 10, 0, tzinfo=TZ),
            to=datetime(2026, 9, 2, 11, 0, tzinfo=TZ),
            granularity="1h",
        )
        assert len(series) == 1  # 两行同落 10:00-11:00 槽
        point = series[0]
        assert point["granularity"] == "1h"
        assert point["window_seconds"] == 3600
        assert point["requests"] == 15
        assert point["qps"] == round(15 / 3600, 3)
        assert point["tps"] == round(400 / 3600, 3)
        assert point["tpm"] == round(400 / 60, 1)

    async def test_series_auto_granularity_for_short_window(self):
        s = _session(
            _row(1, bucket=datetime(2026, 9, 2, 10, 5, tzinfo=TZ), deployment_id="d-1", status="success", requests=1)
        )
        series = await mqs.dimension_series(
            s,
            dim="deployment",
            dim_id="d-1",
            from_=datetime(2026, 9, 2, 10, 0, tzinfo=TZ),
            to=datetime(2026, 9, 2, 11, 0, tzinfo=TZ),
        )
        assert series[0]["granularity"] == "5m"  # ≤6h → 5min

    async def test_series_bad_dim_or_window_empty(self):
        s = _session(
            _row(1, bucket=datetime(2026, 9, 2, 10, 5, tzinfo=TZ), deployment_id="d-1", status="success", requests=1)
        )
        assert await mqs.dimension_series(s, dim="nope", dim_id="x", from_=FROM, to=TO) == []
        assert await mqs.dimension_series(s, dim="deployment", dim_id="d-1", from_=TO, to=FROM) == []


class TestMergeAndPercentileEdges:
    def test_interrupted_classified_separately(self):
        rows = [
            _row(1, bucket=NOW, status="interrupted", requests=4),
            _row(2, bucket=NOW, status="success", requests=6),
        ]
        merged = mqs.merge_bucket_rows(rows)
        assert merged["requests"] == 10
        assert merged["interrupted"] == 4
        assert merged["errors"] == 0

    def test_merge_buckets_truncates_overlong_source(self):
        target = [1] * 13
        mqs._merge_buckets(target, [1] * 20)  # 超长 source 到 target 长度即止
        assert target == [2] * 13
        before = list(target)
        mqs._merge_buckets(target, None)
        assert target == before

    def test_short_rows_skipped_in_percentile(self):
        assert mqs.percentile_from_buckets([1, 2], (10, 20, 30), 0.5) is None  # 长度不足


class _CapturingSession:
    """只捕获语句、不执行：给 upstream_endpoint_rows 喂一条空结果。"""

    def __init__(self) -> None:
        self.stmt = None

    async def execute(self, stmt):
        self.stmt = stmt
        return _FakeResult([])


class TestUpstreamEndpointRowsQueryShape:
    """外部上游明细的取数口径（**语句形状锁**，刻意不做行为断言）。

    这条查询带 ``outerjoin`` + ``coalesce``，而测试桩按项目约定不支持 join，也无法求值
    SQL 表达式（见 ``_op_session`` 模块 docstring），行为级验证只能上真库——不在此处破例。
    故直接编译语句断言口径，断的正是两处会让「明细丢行」的写法，各自对应一个线上症状。
    """

    async def test_uses_endpoint_name_and_left_join(self):
        db = _CapturingSession()
        await mqs.upstream_endpoint_rows(db, FROM, TO)
        sql = str(db.stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        sql_upper = sql.upper()

        # 取端点 name 而非 provider_name：后者选填，这里过去配一条 IS NOT NULL 过滤，
        # 没填供应商的外部端点会**整个从明细消失**（请求量仍进「外部上游」汇总卡，两边对不上）。
        assert "model_endpoint.name" in sql
        assert "model_endpoint.provider_name" not in sql
        # LEFT JOIN 而非 JOIN：端点被删后其历史桶 join 不上会整批掉行，同样造成明细合计
        # 小于汇总卡；掉行归入兜底标签。
        assert "LEFT OUTER JOIN" in sql_upper
        assert "JOIN MODEL_ENDPOINT" in sql_upper
        assert mqs._DELETED_ENDPOINT_LABEL in sql
