"""租户端数据看板路由契约测试：401 外层门禁 / 平台域 403 / 整屏聚合契约与租户作用域。"""

from __future__ import annotations

from httpx import AsyncClient
from _fakes import admin_session, tenant_session

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user


class TestTenantDashboardRoutes:
    async def test_routes_require_auth(self, client: AsyncClient):
        """无登录态 → 401。"""
        r = await client.get("/api/v1/tenant/dashboard/screen")
        assert r.status_code == 401

    async def test_platform_session_rejected_by_gate(self, client: AsyncClient):
        """平台域会话过不了 require_tenant_access 外层门禁 → 403。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        try:
            r = await client.get("/api/v1/tenant/dashboard/screen")
            assert r.status_code == 403
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    async def test_screen_scopes_to_current_tenant(self, client: AsyncClient, monkeypatch):
        """整屏聚合：租户作用域强制（tenant_id 取自会话）+ 窗口参数透传。"""
        from llmpod.apps.webserver.routes.tenant import dashboard as dash_route

        captured: dict = {}

        async def _fake(db, tenant_id, *, from_, to):
            captured.update(tenant_id=tenant_id, **{"from": from_, "to": to})
            return _screen_payload()

        monkeypatch.setattr(dash_route.svc, "tenant_screen", _fake)
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        try:
            r = await client.get(
                "/api/v1/tenant/dashboard/screen?from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z",
            )
            assert r.status_code == 200
            body = r.json()
            assert body["code"] == 200
            data = body["data"]
            assert data["calls"] == 12345
            assert data["month_consume"] == "89.20"
            assert data["trend"][0]["call_count"] == 1800
            assert data["models"][0]["name"] == "DeepSeek-V3"
            assert data["recent_calls"][0]["model"] == "qwen-max"
            assert data["available"] == "134.50"
            assert captured["tenant_id"] == 5  # 作用域 = 会话租户
            assert captured["from"].month == 9 and captured["to"].day == 2
        finally:
            app.dependency_overrides.pop(get_current_user, None)


def _screen_payload():
    return {
        "calls": 12345,
        "active_models": 3,
        "error_rate": 0.002,
        "total_tokens": 9_800_000,
        "avg_ttft_ms": 145,
        "trend": [{"date": "2026-09-05", "call_count": 1800, "error_rate": 0.001}],
        "models": [{"id": 1, "name": "DeepSeek-V3"}],
        "recent_calls": [
            {
                "model": "qwen-max",
                "input_tokens": 100,
                "output_tokens": 200,
                "latency_ms": 320,
                "status": "success",
                "created_at": "2026-09-05T10:00:00+08:00",
            }
        ],
        "balance": "234.50",
        "frozen_balance": "100.00",
        "available": "134.50",
        "month_consume": "89.20",
    }
