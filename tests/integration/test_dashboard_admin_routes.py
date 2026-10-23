"""admin dashboard 路由契约测试（stub 驱动，遵循项目 HTTP 层测试原则）。

覆盖：/dashboard 权限门（401 无登录 / 200 超管）、Result 契约、
service 层 monkeypatch 返回 canned 数据（summary/trend/models/tenants）。
fixture 用 conftest：`client` 不覆盖鉴权（401 用例），`admin_client` 带超管。
"""

from __future__ import annotations

import pytest


class TestDashboardRoutes:
    async def test_summary_unauthorized_401(self, client):
        """无登录态 → 401（require_platform_role 外层门禁）。"""
        resp = await client.get("/api/v1/admin/dashboard/summary")
        assert resp.status_code == 401

    async def test_summary_returns_cards(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        async def _fake_summary(*args, **kwargs):
            return {
                "calls": 2350000,
                "active_models": 12,
                "active_tenants": 8,
                "avg_ttft_ms": 185,
                "error_rate": 0.003,
                "total_tokens": 12500000,
                "online_nodes": 23,
                "total_nodes": 30,
            }

        monkeypatch.setattr(dash_route.svc, "summary", _fake_summary)
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/summary?from=2026-08-28T00:00:00Z&to=2026-08-28T12:00:00Z"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        data = body["data"]
        assert data["calls"] == 2350000
        assert data["avg_ttft_ms"] == 185
        assert data["online_nodes"] == 23

    async def test_trend_returns_points(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        async def _fake_trend(*args, **kwargs):
            return [{"date": "2026-06-20", "call_count": 18000, "error_rate": 0.004}]

        monkeypatch.setattr(dash_route.svc, "trend", _fake_trend)
        resp = await admin_client.get("/api/v1/admin/dashboard/trend?days=14")
        body = resp.json()
        assert body["code"] == 200
        assert body["data"][0]["date"] == "2026-06-20"
        assert body["data"][0]["call_count"] == 18000

    async def test_models_returns_ranking(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        async def _fake_models(*args, **kwargs):
            return [
                {
                    "model": "DeepSeek-V3",
                    "call_count": 1250000,
                    "ratio": 0.49,
                    "avg_ttft_ms": 185,
                    "error_rate": 0.002,
                    "tokens": 12500000,
                }
            ]

        monkeypatch.setattr(dash_route.svc, "model_ranking", _fake_models)
        resp = await admin_client.get("/api/v1/admin/dashboard/models?from=2026-08-28T00:00:00Z")
        body = resp.json()
        assert body["code"] == 200
        item = body["data"][0]
        assert item["model"] == "DeepSeek-V3"
        assert item["ratio"] == 0.49
        assert "gpu_util" not in item  # GPU 利用率列已移除，契约不含该字段

    async def test_tenants_returns_ranking(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        async def _fake_tenants(*args, **kwargs):
            return [{"tenant_id": 5, "tenant": "企业 A", "call_count": 1250000, "ratio": 0.49}]

        monkeypatch.setattr(dash_route.svc, "tenant_ranking", _fake_tenants)
        resp = await admin_client.get("/api/v1/admin/dashboard/tenants?from=2026-08-28T00:00:00Z")
        body = resp.json()
        assert body["code"] == 200
        item = body["data"][0]
        assert item["tenant"] == "企业 A"
        assert item["tenant_id"] == 5
        assert item["ratio"] == 0.49


class TestScreenRoute:
    async def test_screen_unauthorized_401(self, client):
        """无登录态 → 401。"""
        resp = await client.get("/api/v1/admin/dashboard/screen")
        assert resp.status_code == 401

    async def test_screen_returns_full_aggregate(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        monkeypatch.setattr(dash_route.screen_svc, "screen", _screen_stub())
        resp = await admin_client.get("/api/v1/admin/dashboard/screen")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["active_tenants"] == 46
        assert data["gpu_online"] == 38 and data["gpu_total"] == 45
        assert data["clusters"][0]["gpu_utilization"] == 57.5
        assert data["deployments"] == {"running": 6, "creating": 1, "stopped": 1, "failed": 0}
        assert data["recent_deployments"][0]["name"] == "DeepSeek-V3"
        assert data["events"][0]["type"] == "success"

    async def test_screen_forwards_window_params(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        captured: dict = {}

        async def _fake(db, *, from_, to):
            captured["from"], captured["to"] = from_, to
            return _screen_payload()

        monkeypatch.setattr(dash_route.screen_svc, "screen", _fake)
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/screen?from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z"
        )
        assert resp.status_code == 200
        assert captured["from"].year == 2026 and captured["from"].month == 9 and captured["from"].day == 1
        assert captured["to"].day == 2


def _screen_payload(**overrides):
    """大屏整屏 canned 数据（满值；测试按需覆写为零值）。"""
    payload = {
        "calls": 42500000,
        "active_models": 12,
        "active_tenants": 46,
        "avg_ttft_ms": 185,
        "error_rate": 0.003,
        "total_tokens": 125000000,
        "online_nodes": 42,
        "total_nodes": 50,
        "gpu_online": 38,
        "gpu_total": 45,
        "clusters": [
            {
                "id": 1,
                "name": "gpu-shared",
                "node_count": 4,
                "online_node_count": 3,
                "gpu_count": 18,
                "gpu_utilization": 57.5,
                "used_vram_gb": 640.2,
                "total_vram_gb": 1440.0,
            }
        ],
        "deployments": {"running": 6, "creating": 1, "stopped": 1, "failed": 0},
        "recent_deployments": [{"id": "dep1", "name": "DeepSeek-V3", "status": "running"}],
        "events": [{"type": "success", "text": "tester 创建了 集群 1", "time": "2026-09-05 10:00:00"}],
    }
    payload.update(overrides)
    return payload


def _screen_stub():
    """返回满值 payload 的 service 桩（窗口默认解析是被测 service 的职责，桩不预设）。"""

    async def _fake(db, *, from_=None, to=None):
        return _screen_payload()

    return _fake


class TestRealtimeRoutes:
    """/realtime /tokens /scrape-health 契约：stub 驱动 + 需求信号接线验证。"""

    async def test_realtime_unauthorized_401(self, client):
        resp = await client.get("/api/v1/admin/dashboard/realtime")
        assert resp.status_code == 401

    async def test_realtime_invalid_dim_rejected(self, admin_client):
        """非法 dim 由 Literal 校验拒绝（与 /tokens 统一 422，不静默回退成 model）。

        回归：旧实现 /realtime 对拼错的维度返回 200 且画成模型构成，排障无从发现。
        """
        resp = await admin_client.get("/api/v1/admin/dashboard/realtime?dim=endpoint")
        assert resp.status_code == 422
        assert resp.json()["code"] == 422

    async def test_realtime_returns_screen_and_touches_demand(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        touched = []

        async def _fake_realtime(*args, **kwargs):
            return _realtime_payload()

        def _fake_touch():
            touched.append(True)

        monkeypatch.setattr(dash_route.realtime_svc, "realtime", _fake_realtime)
        monkeypatch.setattr(dash_route, "touch_scraper_demand", _fake_touch)
        resp = await admin_client.get("/api/v1/admin/dashboard/realtime?dim=model")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["window"]["source"] == "hot"
        assert data["window"]["age_ms"] == 1200
        assert data["kpi"]["in_flight"]["peak"] == 61
        assert data["trend"]["baseline"]["qpm"] == 412.0
        assert data["composition"]["items"][-1]["key"] == "__other__"
        assert data["engines"][0]["age_ms"] == 1200
        # 消费即触碰：realtime 被调用时必须触碰需求信号（引擎抓取升快档）
        assert touched == [True]

    async def test_realtime_invalid_range_rejected(self, admin_client):
        """反序区间必须 422，不能静默返回一份空屏。

        这与旧实现「非法 window 静默回落 1h」是同一类问题：调用方看到「正常的
        空数据」无从发现参数拼错。
        """
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/realtime?from=2026-09-17T10:00:00%2B08:00&to=2026-09-17T09:00:00%2B08:00"
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == 422

    async def test_realtime_span_over_cap_rejected(self, admin_client):
        """跨度超上限同样 422（上限是唯一的成本防线，静默截断会给出少几天的图）。"""
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/realtime?from=2026-01-01T00:00:00%2B08:00&to=2026-09-17T00:00:00%2B08:00"
        )
        assert resp.status_code == 422

    async def test_realtime_half_compare_window_rejected(self, admin_client):
        """只给 prev_from 不给 prev_to → 422：半个对照窗会静默算出错的 delta。"""
        resp = await admin_client.get("/api/v1/admin/dashboard/realtime?prev_from=2026-09-16T00:00:00%2B08:00")
        assert resp.status_code == 422

    async def test_realtime_range_and_compare_forwarded(self, admin_client, monkeypatch):
        """from/to 与 prev_from/prev_to 原样透传 service（路由不做二次改写）。"""
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        captured: dict = {}

        async def _fake(db, *, from_, to, prev_from=None, prev_to=None, dim, **kw):
            captured.update(from_=from_, to=to, prev_from=prev_from, prev_to=prev_to)
            return _realtime_payload()

        monkeypatch.setattr(dash_route.realtime_svc, "realtime", _fake)
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/realtime"
            "?from=2026-09-10T00:00:00%2B08:00&to=2026-09-17T00:00:00%2B08:00"
            "&prev_from=2026-09-03T00:00:00%2B08:00&prev_to=2026-09-10T00:00:00%2B08:00"
        )
        assert resp.status_code == 200
        assert captured["from_"].isoformat() == "2026-09-10T00:00:00+08:00"
        assert captured["to"].isoformat() == "2026-09-17T00:00:00+08:00"
        assert captured["prev_from"].isoformat() == "2026-09-03T00:00:00+08:00"
        assert captured["prev_to"].isoformat() == "2026-09-10T00:00:00+08:00"

    async def test_realtime_filter_params_forwarded(self, admin_client, monkeypatch):
        """/realtime 的 model/tenant_id/api_key_id 筛选透传 service（下推 SQL）。

        不传时三者都是 None——「不过滤」与「过滤到空」必须能区分开。
        """
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        captured: dict = {}

        async def _fake(
            db, *, from_, to, prev_from=None, prev_to=None, dim, model=None, tenant_id=None, api_key_id=None
        ):
            captured.update(model=model, tenant_id=tenant_id, api_key_id=api_key_id)
            return _realtime_payload()

        monkeypatch.setattr(dash_route.realtime_svc, "realtime", _fake)

        resp = await admin_client.get("/api/v1/admin/dashboard/realtime")
        assert resp.status_code == 200
        assert captured == {"model": None, "tenant_id": None, "api_key_id": None}

        resp = await admin_client.get("/api/v1/admin/dashboard/realtime?model=qwen2.5-7b&tenant_id=3&api_key_id=9")
        assert resp.status_code == 200
        assert captured == {"model": "qwen2.5-7b", "tenant_id": 3, "api_key_id": 9}

    async def test_models_filter_params_forwarded(self, admin_client, monkeypatch):
        """/models 的 model/tenant_id 筛选透传 service（下推 SQL）。"""
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        captured: dict = {}

        async def _fake(db, *, from_, to, top_n, model, tenant_id):
            captured.update(model=model, tenant_id=tenant_id)
            return []

        monkeypatch.setattr(dash_route.svc, "model_ranking", _fake)
        resp = await admin_client.get("/api/v1/admin/dashboard/models?model=qwen2.5-7b&tenant_id=3")
        assert resp.status_code == 200
        assert captured == {"model": "qwen2.5-7b", "tenant_id": 3}

    async def test_tokens_filter_params_forwarded(self, admin_client, monkeypatch):
        """/tokens 的 model/tenant_id 筛选透传 service（维度列之外的列过滤）。"""
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        captured: dict = {}

        async def _fake(db, *, dim, from_, to, top, model, tenant_id):
            captured.update(model=model, tenant_id=tenant_id)
            return {
                "from": "2026-09-15T00:00:00+08:00",
                "to": "2026-09-15T12:00:00+08:00",
                "rows": [],
                "other": None,
                "total": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0},
            }

        monkeypatch.setattr(dash_route.svc, "token_summary", _fake)
        resp = await admin_client.get("/api/v1/admin/dashboard/tokens?model=llama3&tenant_id=5")
        assert resp.status_code == 200
        assert captured == {"model": "llama3", "tenant_id": 5}

    async def test_tokens_returns_summary(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        async def _fake_token_summary(*args, **kwargs):
            return {
                "from": "2026-09-11T00:00:00+08:00",
                "to": "2026-09-11T14:32:07+08:00",
                "rows": [
                    {
                        "key": "qwen2.5-7b",
                        "input_tokens": 5214880,
                        "output_tokens": 1842110,
                        "cached_tokens": 1742330,
                        "total_tokens": 7056990,
                        "ratio": 0.439,
                    },
                ],
                "other": {
                    "key": "__other__",
                    "count": 4,
                    "input_tokens": 1012880,
                    "output_tokens": 364220,
                    "cached_tokens": 402110,
                    "total_tokens": 1377100,
                    "ratio": 0.086,
                },
                "total": {
                    "input_tokens": 11932736,
                    "output_tokens": 4061806,
                    "cached_tokens": 4743200,
                    "total_tokens": 15994542,
                },
            }

        monkeypatch.setattr(dash_route.svc, "token_summary", _fake_token_summary)
        resp = await admin_client.get("/api/v1/admin/dashboard/tokens?dim=model&top=5")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["dim"] == "model"
        assert data["rows"][0]["total_tokens"] == 7056990
        # 口径契约：total = input + output；cached 是 input 子集，不进合计算式
        assert data["total"]["total_tokens"] == data["total"]["input_tokens"] + data["total"]["output_tokens"]
        assert data["total"]["cached_tokens"] <= data["total"]["input_tokens"]
        assert data["other"]["count"] == 4

    async def test_tokens_invalid_dim_rejected(self, admin_client):
        """非法 dim 由 Literal 校验拒绝（统一 422 参数校验失败）。"""
        resp = await admin_client.get("/api/v1/admin/dashboard/tokens?dim=bogus")
        assert resp.status_code == 422
        assert resp.json()["code"] == 422

    async def test_scrape_health_scraper_not_running(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        monkeypatch.setattr(dash_route, "get_scraper", lambda: None)
        resp = await admin_client.get("/api/v1/admin/dashboard/scrape-health")
        assert resp.status_code == 200
        assert resp.json()["data"]["enabled"] is False

    async def test_scrape_health_reports_targets(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route
        from tests.unit.test_metrics_scraper import _cfg

        from llmpod.apps.webserver.metrics_scraper import MetricsScraper

        scraper = MetricsScraper(_cfg())
        scraper._record_health("http://t1/metrics", ok=True, elapsed_ms=15.0)
        monkeypatch.setattr(dash_route, "get_scraper", lambda: scraper)
        resp = await admin_client.get("/api/v1/admin/dashboard/scrape-health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["enabled"] is True
        assert data["proxy_targets"][0]["consecutive_failures"] == 0
        assert data["intervals"]["proxy"] == 1


def _realtime_payload(**overrides):
    """实时整屏 canned 数据（含新增字段：计数 KPI / 分布 / requests 序列）。"""
    payload = {
        "ts": "2026-09-11T14:32:07+08:00",
        "window": {
            "from": "2026-09-11T13:32:07+08:00",
            "to": "2026-09-11T14:32:07+08:00",
            "granularity": "30s",
            "source": "hot",
            "age_ms": 1200,
        },
        "kpi": {
            "qps": {"value": 128.4, "delta_pct": 12.0},
            "tpm": {"value": 42140.0, "delta_pct": 8.3},
            "calls": {"value": 462240, "delta_pct": 9.1},
            "total_tokens_kpi": {"value": 25284000, "delta_pct": 8.3},
            "avg_context": {"value": 5470, "delta": 120},
            "truncation_rate": {"value": 0.014, "delta_pp": 0.3},
            "in_flight": {"value": 37, "peak": 61},
            "error_rate": {"value": 0.0042, "delta_pp": 0.1},
            "p95_ms": {"value": 1240, "delta_ms": -80},
            "cache_hit_rate": {"value": 0.637, "delta_pp": 2.1},
            "latency_avg_ms": {"value": 710, "delta_ms": 12},
            "ttft_avg_ms": {"value": 380, "delta_ms": 50},
            "kv_cache_util": {"value": 0.93, "instance_id": "i-2"},
            "waiting_requests": {"value": 7},
        },
        "trend": {
            "baseline": {"qpm": 412.0, "tpm": 33900.0},
            "points": [
                {
                    "ts": "2026-09-11T13:32:07+08:00",
                    "qpm": 412.0,
                    "tpm": 33900.0,
                    "prompt_tpm": 25400.0,
                    "completion_tpm": 8500.0,
                    "requests": 41200,
                    "latency_avg_ms": 690,
                }
            ],
        },
        "composition": {
            "dim": "model",
            "total": [{"ts": "2026-09-11T13:32:07+08:00", "tpm": 42140.0}],
            "items": [
                {
                    "key": "qwen2.5-7b",
                    "tpm": [{"ts": "2026-09-11T13:32:07+08:00", "value": 26295.0}],
                    "requests": [{"ts": "2026-09-11T13:32:07+08:00", "value": 21000}],
                },
                {
                    "key": "__other__",
                    "tpm": [{"ts": "2026-09-11T13:32:07+08:00", "value": 970.0}],
                    "requests": [{"ts": "2026-09-11T13:32:07+08:00", "value": 1200}],
                },
            ],
        },
        "distribution": None,
        "engines": [
            {
                "instance_id": "i-1",
                "deployment_id": "dep-1",
                "deployment_name": "qwen",
                "engine": "vllm",
                "rank": 0,
                "status": "RUNNING",
                "ok": True,
                "kv_cache_util": 0.68,
                "running_requests": 4,
                "waiting_requests": 0,
                "prefix_cache_hit_rate": 0.712,
                "num_preemptions": 0,
                "age_ms": 1200,
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestOverviewRoute:
    """/overview 契约：权限门 + 整屏字段。"""

    async def test_overview_unauthorized_401(self, client):
        resp = await client.get("/api/v1/admin/dashboard/overview")
        assert resp.status_code == 401

    async def test_overview_returns_full_screen(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        async def _fake(db):
            return {
                "ts": "2026-09-15T11:00:00+08:00",
                "window": {"from": "2026-09-15T00:00:00+08:00", "to": "2026-09-15T11:00:00+08:00"},
                "kpi": {
                    "calls": {"value": 128412, "delta_pct": 12.0},
                    "active_tenants": {"value": 38, "delta": 2},
                    "active_models": {"value": 12, "delta": 1},
                    "new_tenants": {"value": 3, "week_total": 8},
                    "success_rate": {"value": 0.9924, "delta_pp": -0.4},
                    "avg_ttft_ms": {"value": 610, "delta_ms": -20},
                },
                "trend": {
                    "points": [{"hour": 10, "calls": 8210, "calls_yesterday": 7330}],
                    "today_total": 128412,
                    "yesterday_same_period_total": 114653,
                },
                "upstream": {
                    "local": {"share": 0.812, "success_rate": 0.9962},
                    "external": {"share": 0.188, "success_rate": 0.916},
                    "providers": [{"name": "xfyun", "share": 0.421, "success_rate": 0.842}],
                },
                "service": {
                    "online_models": 12,
                    "replicas": 34,
                    "engine_mix": {"vllm": 24, "sglang": 8, "llamacpp": 2},
                    "deployments": {"running": 18, "creating": 2, "stopped": 5, "failed": 1},
                    "recent_deployments": [{"id": "dep-1", "name": "qwen", "status": "running"}],
                    "nodes": {"online": 12, "total": 12},
                    "gpu": {"online": 34, "total": 48},
                    "clusters": [],
                },
            }

        monkeypatch.setattr(dash_route.overview_svc, "overview", _fake)
        resp = await admin_client.get("/api/v1/admin/dashboard/overview")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kpi"]["new_tenants"]["week_total"] == 8
        assert data["trend"]["points"][0]["calls_yesterday"] == 7330
        assert data["upstream"]["providers"][0]["name"] == "xfyun"
        assert data["service"]["engine_mix"]["vllm"] == 24


class TestEngineHistoryRoute:
    """/engines/history 契约：权限门 + 窗口词表 + 序列形状。"""

    async def test_engine_history_unauthorized_401(self, client):
        resp = await client.get("/api/v1/admin/dashboard/engines/history")
        assert resp.status_code == 401

    async def test_engine_history_invalid_range_422(self, admin_client):
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/engines/history?from=2026-09-17T10:00:00%2B08:00&to=2026-09-17T09:00:00%2B08:00"
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == 422

    async def test_engine_history_returns_series(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        captured: dict = {}

        async def _fake(db, *, from_, to):
            captured["range"] = (from_, to)
            return {
                "window": {"from": "2026-09-15T10:00:00+08:00", "to": "2026-09-15T11:00:00+08:00", "granularity": "1h"},
                "instances": [
                    {
                        "instance_id": "i-1",
                        "deployment_id": "dep-1",
                        "deployment_name": "qwen",
                        "engine": "vllm",
                        "rank": 0,
                        "status": "RUNNING",
                        "series": [
                            {
                                "ts": "2026-09-15T10:00:00+08:00",
                                "kv_avg": 0.71,
                                "kv_max": 0.83,
                                "preemptions": 0,
                                "samples": 144,
                            }
                        ],
                    }
                ],
            }

        monkeypatch.setattr(dash_route.realtime_svc, "engine_history", _fake)
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/engines/history?from=2026-09-15T10:00:00%2B08:00&to=2026-09-15T11:00:00%2B08:00"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert captured["range"][0].isoformat() == "2026-09-15T10:00:00+08:00"
        assert captured["range"][1].isoformat() == "2026-09-15T11:00:00+08:00"
        assert data["instances"][0]["series"][0]["samples"] == 144
        # 格宽随跨度走：值必须原样透传（不能是写死的 5m）
        assert data["window"]["granularity"] == "1h"


class TestAnomalyRoute:
    """/anomaly 契约：权限门 + 窗口词表 + 面板形状。"""

    async def test_anomaly_unauthorized_401(self, client):
        resp = await client.get("/api/v1/admin/dashboard/anomaly")
        assert resp.status_code == 401

    async def test_anomaly_invalid_range_422(self, admin_client):
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/anomaly?from=2026-09-17T10:00:00%2B08:00&to=2026-09-17T09:00:00%2B08:00"
        )
        assert resp.status_code == 422

    async def test_anomaly_returns_items_and_upstream(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        captured: dict = {}

        async def _fake(db, *, from_, to):
            captured["range"] = (from_, to)
            return {
                "ts": "2026-09-15T11:00:00+08:00",
                "window": {"from": "2026-09-15T10:00:00+08:00", "to": "2026-09-15T11:00:00+08:00"},
                "anomaly": {
                    "total": 2921,
                    "ratio": 0.0048,
                    "items": [
                        {"kind": "rate_limited", "count": 44, "share": 0.015, "side": "rejected"},
                        {"kind": "upstream_timeout", "count": 1204, "share": 0.412, "side": "error"},
                    ],
                },
                "upstream": {
                    "local": {"success_rate": 0.9962, "latency_avg_ms": 620, "qpm": 112.0},
                    "external": {"success_rate": 0.916, "latency_avg_ms": 1850, "qpm": 16.0},
                    "providers": [{"name": "xfyun", "success_rate": 0.842, "latency_avg_ms": 2600, "qpm": 4.0}],
                },
            }

        monkeypatch.setattr(dash_route.anomaly_svc, "anomaly", _fake)
        resp = await admin_client.get(
            "/api/v1/admin/dashboard/anomaly?from=2026-09-10T00:00:00%2B08:00&to=2026-09-17T00:00:00%2B08:00"
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert captured["range"][0].isoformat() == "2026-09-10T00:00:00+08:00"
        assert data["anomaly"]["items"][0]["side"] == "rejected"
        assert data["upstream"]["providers"][0]["name"] == "xfyun"


class TestDashboardRangeContract:
    """三个大屏端点共用一套区间参数 —— 同屏一个区间控件，契约不许再分叉。

    参数面曾经是「/realtime 一套窗口词表、热力图+异常另一套」，同屏两个控件、
    切一个只有一半面板跟着动；区间化之后这类分叉从契约里消失。
    """

    PATHS = ("/realtime", "/engines/history", "/anomaly")
    RANGE = "?from=2026-09-10T00:00:00%2B08:00&to=2026-09-17T00:00:00%2B08:00"

    @staticmethod
    def _patch(monkeypatch, captured):
        from llmpod.apps.webserver.routes.admin import dashboard as dash_route

        async def _fake_realtime(db, *, from_, to, prev_from=None, prev_to=None, dim, **kw):
            captured["range"] = (from_, to)
            return _realtime_payload()

        async def _fake_history(db, *, from_, to):
            captured["range"] = (from_, to)
            return {"window": {"from": "x", "to": "y", "granularity": "5m"}, "instances": []}

        async def _fake_anomaly(db, *, from_, to):
            captured["range"] = (from_, to)
            return {
                "ts": "x",
                "window": {"from": "x", "to": "y"},
                "anomaly": {"total": 0, "ratio": 0.0, "items": []},
                "upstream": {"local": None, "external": None, "providers": []},
            }

        monkeypatch.setattr(dash_route.realtime_svc, "realtime", _fake_realtime)
        monkeypatch.setattr(dash_route.realtime_svc, "engine_history", _fake_history)
        monkeypatch.setattr(dash_route.anomaly_svc, "anomaly", _fake_anomaly)

    @pytest.mark.parametrize("path", PATHS)
    async def test_range_reaches_every_endpoint(self, admin_client, monkeypatch, path):
        captured: dict = {}
        self._patch(monkeypatch, captured)

        resp = await admin_client.get(f"/api/v1/admin/dashboard{path}{self.RANGE}")

        assert resp.status_code == 200, resp.text
        assert captured["range"][1].isoformat() == "2026-09-17T00:00:00+08:00"

    @pytest.mark.parametrize("path", PATHS)
    async def test_removed_window_param_is_rejected(self, admin_client, monkeypatch, path):
        """已下线的 `window` 必须**显式 422**，不能静默忽略。

        FastAPI 对未声明的查询参数是静默丢弃的：没跟上的调用方（旧前端发
        `window=1h`）会拿到 200 + 今日全量数据，屏 3 选「近1h」实际显示今天一整天、
        屏 2 的 KPI 环比全是 `--`，而页面上看不出任何异常。把这份静默变成一次
        可见的失败，正是仓内「非法参数要能从 422 里发现」的既有约定。
        """
        self._patch(monkeypatch, {})

        resp = await admin_client.get(f"/api/v1/admin/dashboard{path}?window=7d")

        assert resp.status_code == 422
        assert resp.json()["error_code"] == "VALIDATION_ERROR"

    async def test_removed_granularity_param_is_rejected(self, admin_client, monkeypatch):
        """`granularity` 同样已下线（粒度改由后端按跨度定）。"""
        self._patch(monkeypatch, {})

        resp = await admin_client.get("/api/v1/admin/dashboard/realtime?granularity=5m")

        assert resp.status_code == 422

    @pytest.mark.parametrize("path", PATHS)
    async def test_reversed_range_rejected(self, admin_client, path):
        """反序区间 422。

        刻意**不 stub service**：区间校验就在 service 第一行（碰 DB 之前），
        把 service 换成桩等于把被测的那层也换掉了——这条用例的价值正在于
        走真服务确认「非法区间不会被静默当成空数据返回 200」。
        """
        resp = await admin_client.get(
            f"/api/v1/admin/dashboard{path}?from=2026-09-17T10:00:00%2B08:00&to=2026-09-17T09:00:00%2B08:00"
        )

        assert resp.status_code == 422
        assert resp.json()["code"] == 422


class TestEngineStatusRoute:
    """/model-deployments/{id}/engine-status：需求信号接线 + 快照年龄。"""

    async def test_touches_demand_and_reports_age_ms(self, admin_client, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from llmpod.app import app
        from llmpod.apps.webserver import metrics_scraper as ms_mod
        from llmpod.db import get_db
        from llmpod.models import Instance
        from tests.integration._fakes import FakeSession

        from tests.unit.test_metrics_scraper import _cfg

        now = datetime.now(timezone.utc)
        scraper = ms_mod.MetricsScraper(_cfg())
        scraper._engine_snapshots["i-1"] = ms_mod.EngineSnapshot(
            instance_id="i-1",
            deployment_id="dep-1",
            ok=True,
            kv_cache_util=0.42,
            fetched_at=now - timedelta(seconds=3),
        )

        touched = []
        monkeypatch.setattr(ms_mod, "get_scraper", lambda: scraper)
        monkeypatch.setattr(ms_mod, "touch_scraper_demand", lambda: touched.append(True))

        session = FakeSession()
        session.preload(
            Instance(
                instance_id="i-1",
                rank=0,
                status="RUNNING",
                deployment_id="dep-1",
                endpoint="http://10.0.0.5:8000",
                spec={},
            )
        )
        session.preload(Instance(instance_id="i-2", rank=1, status="RUNNING", deployment_id="dep-1", spec={}))

        async def _fake_db():
            yield session

        app.dependency_overrides[get_db] = _fake_db
        try:
            resp = await admin_client.get("/api/v1/admin/model-deployments/dep-1/engine-status")
        finally:
            app.dependency_overrides.pop(get_db, None)
        assert resp.status_code == 200
        items = {i["instance_id"]: i for i in resp.json()["data"]}
        assert touched == [True]  # 消费即触碰
        age = items["i-1"]["age_ms"]
        assert age is not None and 2500 <= age < 60000  # ≈3s 前的快照
        assert items["i-1"]["kv_cache_util"] == pytest.approx(0.42)
        assert items["i-1"]["metrics_expected"] is True  # 有端点的实例应出指标
        # 从未抓到的实例：ok=False 且指标全空（不显示 0）
        assert items["i-2"]["ok"] is False
        assert items["i-2"]["kv_cache_util"] is None
        assert items["i-2"]["age_ms"] is None
        # endpoint 为空（headless worker）：指标缺失属预期，前端据此区分结构性与故障
        assert items["i-2"]["metrics_expected"] is False
