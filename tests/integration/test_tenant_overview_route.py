"""租户概览聚合路由测试：/admin/tenants/overview 契约 + 路径不被 /{tenant_id} 吞掉。"""

from __future__ import annotations

from httpx import AsyncClient


def _canned_overview():
    return {
        "total_tenants": 7,
        "active_tenants": 5,
        "disabled_tenants": 2,
        "total_users": 69,
        "dates": ["2026-08-23", "2026-08-24"],
        "consume_trend": [
            {"tenant_id": 1, "tenant_name": "企业A", "data": [2650.0, 2700.0]},
        ],
        "balance_top": [
            {"tenant_id": 3, "tenant_name": "企业C", "balance": "5200.000000", "available": "5000.000000"},
        ],
    }


class TestTenantOverviewRoute:
    async def test_requires_auth(self, client: AsyncClient):
        """无登录态 → 401。"""
        resp = await client.get("/api/v1/admin/tenants/overview")
        assert resp.status_code == 401

    async def test_overview_returns_aggregate(self, admin_client, monkeypatch):
        """字面路径 /overview 优先于 /{tenant_id} 匹配（注册顺序保证）。"""
        from llmpod.apps.webserver.routes.admin import tenants as tenants_route

        async def _fake(db, *, days, top_n):
            return _canned_overview()

        monkeypatch.setattr(tenants_route, "tenant_overview", _fake)
        resp = await admin_client.get("/api/v1/admin/tenants/overview")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_tenants"] == 7
        assert data["disabled_tenants"] == 2
        assert data["consume_trend"][0]["data"] == [2650.0, 2700.0]
        assert data["balance_top"][0]["available"] == "5000.000000"

    async def test_overview_forwards_days_and_top_n(self, admin_client, monkeypatch):
        from llmpod.apps.webserver.routes.admin import tenants as tenants_route

        captured: dict = {}

        async def _fake(db, *, days, top_n):
            captured.update(days=days, top_n=top_n)
            return _canned_overview()

        monkeypatch.setattr(tenants_route, "tenant_overview", _fake)
        resp = await admin_client.get("/api/v1/admin/tenants/overview?days=7&top_n=3")
        assert resp.status_code == 200
        assert captured == {"days": 7, "top_n": 3}
