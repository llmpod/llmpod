"""stop 部署端点契约测试：验证 stop 联动下架（unpublish_deployment_routes 被调用）。

遵循「stub get_db + 鉴权覆盖」：client fixture 已覆盖 get_db（_StubSession），本测试
monkeypatch 各 service/orchestrator/StopService 桩，验证 stop 路由在停止链路末尾、
commit 前调用 unpublish_deployment_routes 下架自动发布路由。
"""

from __future__ import annotations

from types import SimpleNamespace

from httpx import AsyncClient

from _fakes import admin_session
from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData


def _admin_user() -> SessionData:
    return admin_session()


class _FakeOrch:
    async def request_cancel(self, deployment_id):
        pass

    async def await_deploy(self, deployment_id, timeout=30.0):
        pass

    async def release_vram(self, deployment_id):
        pass


class _FakeStopService:
    async def stop_instances(self, deployment_id):
        pass


class TestStopRoute:
    async def test_stop_unpublishes_deployment_routes(self, client: AsyncClient, monkeypatch):
        from llmpod.services import deploy_log_service, deployment_service

        app.dependency_overrides[get_current_user] = _admin_user

        dep = SimpleNamespace(deployment_id="dep-1", status="running", health_status="healthy")

        async def _load(db, deployment_id):
            return dep, None

        async def _list_instances(db, deployment_id):
            return []

        async def _append_log(db, **kwargs):
            pass

        async def _set_phase(db, deployment_id, phase, **kwargs):
            pass

        async def _delete_assignments(db, deployment_id):
            pass

        unpublish_calls: list[str] = []

        async def _unpublish(db, deployment_id):
            unpublish_calls.append(deployment_id)

        async def _recompute_demand(self):
            pass

        monkeypatch.setattr(deployment_service, "load_deployment", _load)
        monkeypatch.setattr(deployment_service, "list_instances", _list_instances)
        monkeypatch.setattr(deploy_log_service, "append_log", _append_log)
        monkeypatch.setattr(deployment_service, "set_phase", _set_phase)
        monkeypatch.setattr(deployment_service, "delete_assignments", _delete_assignments)
        monkeypatch.setattr(deployment_service, "unpublish_deployment_routes", _unpublish)
        monkeypatch.setattr("llmpod.apps.manager.deployment.orchestrator.get_orchestrator", lambda: _FakeOrch())
        monkeypatch.setattr("llmpod.apps.manager.deployment.stop.StopService", lambda: _FakeStopService())
        monkeypatch.setattr(
            "llmpod.apps.manager.deployment.placement.PlacementService.recompute_demand",
            _recompute_demand,
        )

        r = await client.post("/api/v1/admin/model-deployments/dep-1/stop")

        assert r.status_code == 200
        body = r.json()
        assert body["data"]["status"] == "stopped"
        assert unpublish_calls == ["dep-1"]
