"""实例启动命令 API 契约测试（GET /admin/model-deployments/{id}/instances/{iid}/command）。

路由层只验：注册/鉴权 401/Result 契约/404 映射；task 快照查询 + docker run 渲染
的编排逻辑由 dispatch.instance_start_command 单测覆盖。
"""

from __future__ import annotations

from httpx import AsyncClient
from _fakes import admin_session

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user


class TestInstanceCommandRoutes:
    PATH = "/api/v1/admin/model-deployments/{dep}/instances/{inst}/command"

    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get(self.PATH.format(dep="d1", inst="i0"))
        assert resp.status_code == 401

    async def test_returns_rendered_command(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        import llmpod.apps.manager.dispatch as dispatch_mod

        async def _fake(db, deployment_id: str, instance_id: str):
            return {
                "found": True,
                "command": "docker run -d \\\n  --name llmpod-d1-i0 vllm/vllm-openai:v0.23.0",
                "image": "vllm/vllm-openai:v0.23.0",
                "container_name": "llmpod-d1-i0",
                "task_status": "SUCCESS",
                "attempt": 1,
                "dispatched_at": "2026-09-07T12:00:00+00:00",
            }

        monkeypatch.setattr(dispatch_mod, "instance_start_command", _fake)

        resp = await client.get(self.PATH.format(dep="d1", inst="i0"))

        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        data = body["data"]
        assert data["found"] is True
        assert data["command"].startswith("docker run -d")
        assert data["image"] == "vllm/vllm-openai:v0.23.0"
        assert data["task_status"] == "SUCCESS"

    async def test_found_false_envelope(self, client: AsyncClient, monkeypatch):
        """无 task 行 → 200 + found=false（重启窗口是正常态，不是错误）。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        import llmpod.apps.manager.dispatch as dispatch_mod

        async def _fake(db, deployment_id: str, instance_id: str):
            return {
                "found": False,
                "command": None,
                "image": None,
                "container_name": None,
                "task_status": None,
                "attempt": None,
                "dispatched_at": None,
            }

        monkeypatch.setattr(dispatch_mod, "instance_start_command", _fake)

        resp = await client.get(self.PATH.format(dep="d1", inst="i0"))
        assert resp.status_code == 200
        assert resp.json()["data"]["found"] is False

    async def test_not_found_maps_404(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        import llmpod.apps.manager.dispatch as dispatch_mod
        from llmpod.core.exceptions import NotFound

        async def _raise(db, deployment_id: str, instance_id: str):
            raise NotFound("ModelDeployment", deployment_id)

        monkeypatch.setattr(dispatch_mod, "instance_start_command", _raise)

        resp = await client.get(self.PATH.format(dep="nope", inst="i0"))
        assert resp.status_code == 404
