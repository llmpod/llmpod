"""start 部署端点契约测试：鉴权 + 成功 + 原卡占用 409（occupied 明细）+ force 透传。

遵循「stub get_db + 鉴权覆盖」：client fixture 已覆盖 get_db；本测试 monkeypatch
orchestrator.get_orchestrator 返回桩，验证路由注册、响应契约、Conflict.details 透传。
"""

from __future__ import annotations

from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.core.exceptions import Conflict


def _admin_user() -> SessionData:
    return admin_session()


class _FakeOrch:
    """桩 orchestrator.restart：记录调用；可注入 Conflict（带 occupied 明细）。"""

    def __init__(self, *, raise_conflict: bool = False):
        self.calls: list[dict] = []
        self.raise_conflict = raise_conflict

    async def restart(self, deployment_id, *, force=False, trigger="system"):
        self.calls.append({"deployment_id": deployment_id, "force": force, "trigger": trigger})
        if self.raise_conflict:
            exc = Conflict("原卡已被其他部署占用，如需继续请使用强制启动")
            exc.details = {"occupied": [{"gpu_uuid": "g1", "by": [{"deployment_id": "other", "used_vram_gb": 8.0}]}]}
            raise exc


class TestStartRoute:
    async def test_no_token_returns_401(self, client: AsyncClient):
        r = await client.post("/api/v1/admin/model-deployments/dep-1/start")
        assert r.status_code == 401

    async def test_start_success(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user
        fake = _FakeOrch()
        monkeypatch.setattr("llmpod.apps.manager.deployment.orchestrator.get_orchestrator", lambda: fake)
        r = await client.post("/api/v1/admin/model-deployments/dep-1/start")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 200 and body["data"]["status"] == "creating"
        assert fake.calls == [{"deployment_id": "dep-1", "force": False, "trigger": "user:1"}]

    async def test_start_force_forwarded(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user
        fake = _FakeOrch()
        monkeypatch.setattr("llmpod.apps.manager.deployment.orchestrator.get_orchestrator", lambda: fake)
        r = await client.post("/api/v1/admin/model-deployments/dep-1/start?force=true")
        assert r.status_code == 200
        assert fake.calls[0]["force"] is True

    async def test_start_conflict_409_with_occupied(self, client: AsyncClient, monkeypatch):
        """原卡被占用 → 409，data.occupied 携带明细供前端提示。"""
        app.dependency_overrides[get_current_user] = _admin_user
        fake = _FakeOrch(raise_conflict=True)
        monkeypatch.setattr("llmpod.apps.manager.deployment.orchestrator.get_orchestrator", lambda: fake)
        r = await client.post("/api/v1/admin/model-deployments/dep-1/start")
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == 409
        assert body["error_code"] == "CONFLICT"
        assert body["data"]["occupied"][0]["gpu_uuid"] == "g1"
        assert body["data"]["occupied"][0]["by"][0]["deployment_id"] == "other"
