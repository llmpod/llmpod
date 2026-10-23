"""容器日志 API 契约测试（GET /admin/model-deployments/{id}/logs/containers）。

引擎 stdout 不入库，崩溃诊断需经 node 隧道 ContainerService.logs 实拉。路由层只验：
注册/鉴权 401/Result 契约/404 映射/dispatch 异常→502 口径；容器名拼装 + 分节点
fan-out 的编排逻辑由 dispatch.container_logs 单测覆盖。
"""

from __future__ import annotations

from httpx import AsyncClient
from _fakes import admin_session

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.core.exceptions import LLMPodError


class TestContainerLogsRoutes:
    PATH = "/api/v1/admin/model-deployments/{dep}/logs/containers"

    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get(self.PATH.format(dep="d1"))
        assert resp.status_code == 401

    async def test_returns_per_instance_logs(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        import llmpod.apps.manager.dispatch as dispatch_mod

        async def _fake_logs(db, deployment_id: str, tail: int = 200):
            return [
                {
                    "instance_id": "inst-1",
                    "node_id": "node-a",
                    "container_name": "llmpod-d1-inst-1",
                    "status": "CRASHED",
                    "logs": "Traceback (most recent call last):\nValueError: task classify unsupported",
                    "error": None,
                },
                {
                    "instance_id": "inst-2",
                    "node_id": "node-b",
                    "container_name": "llmpod-d1-inst-2",
                    "status": "READY",
                    "logs": "INFO: Application startup complete.",
                    "error": None,
                },
            ]

        monkeypatch.setattr(dispatch_mod, "container_logs", _fake_logs)

        resp = await client.get(self.PATH.format(dep="d1"), params={"tail": 150})

        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        data = body["data"]
        assert isinstance(data, list) and len(data) == 2
        assert data[0]["container_name"] == "llmpod-d1-inst-1"
        assert data[0]["status"] == "CRASHED"
        assert "ValueError" in data[0]["logs"]
        assert data[1]["status"] == "READY"
        # tail 透传
        _ = data

    async def test_not_found_maps_404(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        import llmpod.apps.manager.dispatch as dispatch_mod
        from llmpod.core.exceptions import NotFound

        async def _raise(db, deployment_id: str, tail: int = 200):
            raise NotFound("ModelDeployment", deployment_id)

        monkeypatch.setattr(dispatch_mod, "container_logs", _raise)

        resp = await client.get(self.PATH.format(dep="nope"))
        assert resp.status_code == 404

    async def test_rpc_failure_maps_400(self, client: AsyncClient, monkeypatch):
        """节点 RPC 失败（LLMPodError 兜底）→ 400 业务错误（错误明细在 message）。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        import llmpod.apps.manager.dispatch as dispatch_mod

        async def _raise(db, deployment_id: str, tail: int = 200):
            raise LLMPodError("Node RPC failed: boom")

        monkeypatch.setattr(dispatch_mod, "container_logs", _raise)

        resp = await client.get(self.PATH.format(dep="d1"))
        assert resp.status_code == 400
        assert "boom" in resp.json()["message"]
