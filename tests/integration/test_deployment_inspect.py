"""部署 inspect 端点契约测试（stub 驱动，不依赖真实 PG/Redis）。

HTTP 层只验路由注册 + 鉴权依赖链 + 请求 schema（extra=forbid 拒未知字段）+ 422 映射。
真实三层解析 / 容量校验逻辑由 ``tests/unit/test_deployment_inspect_dryrun.py`` 与
``tests/unit/test_probe_placement.py`` 覆盖。
"""

from __future__ import annotations

from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData


def _admin_user() -> SessionData:
    return admin_session()


class TestInspectDeploymentEndpoint:
    async def test_no_auth_returns_401(self, client: AsyncClient):
        """路由已注册 + 受 require_permission 保护（无登录态→401）。"""
        resp = await client.post("/api/v1/admin/model-deployments/inspect", json={"model_id": 1})
        assert resp.status_code == 401

    async def test_missing_model_id_rejected(self, client: AsyncClient):
        """model_id 必填 → 空体 422。"""
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post("/api/v1/admin/model-deployments/inspect", json={})
        assert resp.status_code == 422

    async def test_unknown_field_rejected(self, client: AsyncClient):
        """extra=forbid → 未知字段 422（防拼写错静默丢弃）。"""
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/model-deployments/inspect",
            json={"model_id": 1, "extra_agrs": ["--x"]},  # 拼写错
        )
        assert resp.status_code == 422

    async def test_accepts_new_flat_fields(self, client: AsyncClient):
        """扩字段（engine_params/engine_config/image/image_tag/model_path）被接受（schema 合法）。

        仅验请求 schema 通过——真正查询在触达 DB（Model.get）时由 _StubSession 抛错，
        返回非 422 即证明字段被接受（404/500 均可，不是 422）。
        """
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/model-deployments/inspect",
            json={
                "model_id": 1,
                "engine": "vllm",
                "topology": {"tp": 2},
                "engine_params": {"extra_args": ["--dtype", "half"], "env": {"X": "1"}},
                "engine_config": {"hf_endpoint": "https://hf-mirror.com"},
                "image": None,
                "image_tag": None,
                "model_path": None,
            },
        )
        # 字段合法 → 不因 schema 拒（422）；触达 DB.get(Model) → _StubSession 无实现 → 500/异常映射
        assert resp.status_code != 422

    async def test_accepts_cpu_lane_fields(self, client: AsyncClient):
        """CPU / 混合 offload 字段（allow_cpu/offload_preference/kv_cache）被接受。"""
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/model-deployments/inspect",
            json={
                "model_id": 1,
                "engine": "llamacpp",
                "topology": {"tp": 1},
                "allow_cpu": True,
                "offload_preference": "cpu_only",
                "kv_cache": {"n_parallel": 2},
            },
        )
        assert resp.status_code != 422

    async def test_invalid_offload_preference_rejected(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/model-deployments/inspect",
            json={"model_id": 1, "allow_cpu": True, "offload_preference": "bogus"},
        )
        assert resp.status_code == 422

    async def test_kv_placement_field_rejected(self, client: AsyncClient):
        """KV 落点不可配：旧 payload 里的 mode 字段被 extra=forbid 拒。"""
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/model-deployments/inspect",
            json={"model_id": 1, "kv_cache": {"mode": "cpu"}},
        )
        assert resp.status_code == 422
