"""版本检查更新路由集成测试：登录门、Result 契约、service 接线。

遵循 stub 范式（conftest client 覆盖 get_db）：HTTP 层只验路由注册、
依赖链与 Result 契约；check_update 业务逻辑由单元测试覆盖。
"""

from __future__ import annotations

from datetime import datetime, timezone

from httpx import AsyncClient

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.core.exceptions import ErrorCode
from llmpod.services.version_service import UpdateCheckData


def _session(access_domain: str = "platform") -> SessionData:
    return SessionData(
        user_id=7,
        tenant_id=5 if access_domain == "tenant" else 0,
        username="alice",
        role_codes=["tenant_admin" if access_domain == "tenant" else "operator"],
        access_domain=access_domain,
    )


class TestVersionLatest:
    async def test_anonymous_get_rejected(self, client: AsyncClient):
        """匿名检查更新：401。"""
        response = await client.get("/api/v1/version/latest")
        assert response.status_code == 401
        assert response.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value

    async def test_any_login_gets_result_contract(self, client: AsyncClient, monkeypatch):
        """任意登录态（租户/平台）：200 + Result 契约 + 全字段。"""

        async def fake_check_update(cfg=None):
            return UpdateCheckData(
                current_version="26.9.3",
                latest_version="26.9.4",
                has_update=True,
                update_check_enabled=True,
                checked_at=datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc),
                notes="1. 新增\n2. 修复",
                release_url="https://example.com/changelog",
                published_at=datetime(2026, 9, 4, 10, 0, 0, tzinfo=timezone.utc),
            )

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.version.check_update", fake_check_update)
        app.dependency_overrides[get_current_user] = lambda: _session("tenant")
        response = await client.get("/api/v1/version/latest")
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 200
        data = body["data"]
        assert data["current_version"] == "26.9.3"
        assert data["latest_version"] == "26.9.4"
        assert data["has_update"] is True
        assert data["update_check_enabled"] is True
        assert data["checked_at"] is not None
        assert data["notes"] == "1. 新增\n2. 修复"
        assert data["release_url"] == "https://example.com/changelog"
        assert data["published_at"] is not None
        assert data["error_message"] is None

    async def test_disabled_check_shape(self, client: AsyncClient, monkeypatch):
        """禁用（未配 URL）：恒返回无更新形态，latest == current。"""

        async def fake_check_update(cfg=None):
            return UpdateCheckData(
                current_version="26.9.3",
                latest_version="26.9.3",
                has_update=False,
                update_check_enabled=False,
            )

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.version.check_update", fake_check_update)
        app.dependency_overrides[get_current_user] = lambda: _session("platform")
        response = await client.get("/api/v1/version/latest")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["has_update"] is False
        assert data["latest_version"] == data["current_version"]
        assert data["update_check_enabled"] is False
        assert data["checked_at"] is None
