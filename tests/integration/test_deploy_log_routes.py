"""deploy_log 路由集成测试：2 个只读端点（嵌套在 /model-deployments 下）的响应契约 + 鉴权。

遵循「stub get_db + 鉴权覆盖」：client fixture 已覆盖 get_db；本测试 monkeypatch
deploy_log_service 的读函数返回 canned 数据，验证路由注册、统一响应契约与鉴权。

端点（deploy_log 已合并进 deployment 路由，不再单独 /deploy-logs）：
- GET /admin/model-deployments/{deployment_id}/logs
- GET /admin/model-deployments/{deployment_id}/logs/nodes
"""

from __future__ import annotations

import datetime

from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.models.deploy_log import DeployLog

_NOW = datetime.datetime(2026, 8, 14, 12, tzinfo=datetime.timezone.utc)


def _admin_user() -> SessionData:
    """超管会话：require_permission 见 is_superuser=True 直接短路，不查 DB 权限。"""
    return admin_session()


def _row(**kw) -> DeployLog:
    base = dict(
        id=1,
        created_at=_NOW,
        deployment_id="d1",
        step="start_instance",
        level="info",
        status="running",
        message="ok",
        node_id="n1",
        task_id=None,
        extra=None,
    )
    base.update(kw)
    return DeployLog(**base)


class TestDeployLogRoutes:
    async def test_no_token_returns_401(self, client: AsyncClient):
        """无鉴权 → 401：证明日志路由已注册且受 require_permission 保护。"""
        r = await client.get("/api/v1/admin/model-deployments/d1/logs")
        assert r.status_code == 401

    async def test_list_logs_envelope(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user
        rows = [_row(message="a"), _row(message="b")]

        async def _fake(db, **kw):
            return rows, 2

        monkeypatch.setattr("llmpod.services.deploy_log_service.list_logs", _fake)
        r = await client.get("/api/v1/admin/model-deployments/d1/logs")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 200
        assert body["data"]["total"] == 2
        assert len(body["data"]["items"]) == 2
        assert body["data"]["page"] == 1 and body["data"]["page_size"] == 100
        assert body["data"]["total_pages"] == 1

    async def test_list_logs_filters_forwarded(self, client: AsyncClient, monkeypatch):
        """node_id/step/level 等过滤参数透传给 service。"""
        app.dependency_overrides[get_current_user] = _admin_user
        captured: dict = {}

        async def _fake(db, **kw):
            captured.update(kw)
            return [], 0

        monkeypatch.setattr("llmpod.services.deploy_log_service.list_logs", _fake)
        r = await client.get(
            "/api/v1/admin/model-deployments/d1/logs",
            params={"node_id": "n1", "step": "running", "level": "error"},
        )
        assert r.status_code == 200
        assert captured["deployment_id"] == "d1"
        assert captured["node_id"] == "n1" and captured["step"] == "running" and captured["level"] == "error"

    async def test_node_summary_envelope(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = _admin_user

        # stub 返回 ORM 行（service 层）；投影成 OUT 模型是路由职责
        async def _fake(db, deployment_id):
            return [
                DeployLog(
                    deployment_id=deployment_id,
                    node_id="n1",
                    step="running",
                    status="success",
                    level="info",
                    message="ok",
                    created_at=_NOW,
                )
            ]

        monkeypatch.setattr("llmpod.services.deploy_log_service.latest_log_by_node", _fake)

        async def _fake_display(db, node_ids):
            return {"n1": ("host-1", "10.0.0.1")}

        monkeypatch.setattr("llmpod.services.deploy_log_service.node_display_map", _fake_display)
        r = await client.get("/api/v1/admin/model-deployments/d1/logs/nodes")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == 200
        assert body["data"][0]["node_id"] == "n1"
        assert body["data"][0]["step"] == "running"
        assert body["data"][0]["node_hostname"] == "host-1"
        assert body["data"][0]["node_ip"] == "10.0.0.1"
        assert body["data"][0]["updated_at"] == "2026-08-14T12:00:00+00:00"
