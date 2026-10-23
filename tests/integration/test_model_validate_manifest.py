"""validate-manifest 端点集成测试（HTTP 契约 + 8 规则各一例）。

本仓 HTTP 层测试只验路由注册、依赖链、统一 Result 契约、异常→HTTP 映射；
断开 PG/Redis（conftest stub get_db），鉴权用 is_superuser 短路。validate-manifest
是纯逻辑（零 IO），superuser + get_db stub 即可直跑，无需 patch service。
"""

from __future__ import annotations

from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData


def _admin_user() -> SessionData:
    return admin_session()


def _files(*pairs: tuple[str, int]) -> list[dict]:
    return [{"name": n, "size": s} for n, s in pairs]


class TestValidateManifestRoute:
    async def test_no_auth_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": [{"name": "a.bin", "size": 1}]},
        )
        assert resp.status_code == 401

    async def test_route_registered_under_models(self, client: AsyncClient):
        """端点挂载在 /api/v1/admin/models/validate-manifest（非 /uploads）。"""
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": [{"name": "model.safetensors", "size": 1 << 30}]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert "warnings" in body["data"]

    async def test_empty_files_rejected_422(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": []},
        )
        assert resp.status_code == 422  # min_length=1


class TestValidateManifestWarnings:
    """8 条规则各一例（severity 断言）。"""

    async def test_no_weight_file_fatal(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": _files(("config.json", 100), ("README.md", 100))},
        )
        assert resp.status_code == 200
        ws = {w["code"]: w for w in resp.json()["data"]["warnings"]}
        assert "no_weight_file" in ws
        assert ws["no_weight_file"]["severity"] == "fatal"

    async def test_no_config_or_index_warn(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": _files(("model.safetensors", 1 << 30), ("tokenizer.json", 100))},
        )
        ws = {w["code"]: w for w in resp.json()["data"]["warnings"]}
        assert "no_config_or_index" in ws
        assert ws["no_config_or_index"]["severity"] == "warn"

    async def test_safetensors_shard_incomplete_fatal(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": _files(("model-00001-of-00003.safetensors", 1 << 30))},
        )
        ws = {w["code"]: w for w in resp.json()["data"]["warnings"]}
        assert "safetensors_shard_incomplete" in ws
        assert ws["safetensors_shard_incomplete"]["severity"] == "fatal"

    async def test_gguf_split_incomplete_warn(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": _files(("model-00001-of-00002.gguf", 1 << 30))},
        )
        ws = {w["code"]: w for w in resp.json()["data"]["warnings"]}
        assert "gguf_split_incomplete" in ws
        assert ws["gguf_split_incomplete"]["severity"] == "warn"

    async def test_single_gguf_no_config_info(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": _files(("qwen.gguf", 5 << 30))},
        )
        ws = {w["code"]: w for w in resp.json()["data"]["warnings"]}
        assert "single_gguf_no_config" in ws
        assert ws["single_gguf_no_config"]["severity"] == "info"

    async def test_mixed_layouts_warn(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": _files(("config.json", 100), ("model_index.json", 100), ("model.safetensors", 1 << 30))},
        )
        ws = {w["code"]: w for w in resp.json()["data"]["warnings"]}
        assert "mixed_layouts" in ws
        assert ws["mixed_layouts"]["severity"] == "warn"

    async def test_unsupported_ext_warn(self, client: AsyncClient):
        app.dependency_overrides[get_current_user] = _admin_user
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": _files(("weights.foo", 10 << 20))},
        )
        ws = {w["code"]: w for w in resp.json()["data"]["warnings"]}
        assert "unsupported_ext" in ws
        assert ws["unsupported_ext"]["severity"] == "warn"

    async def test_oversize_fatal(self, client: AsyncClient, monkeypatch):
        from llmpod.config.settings import settings

        app.dependency_overrides[get_current_user] = _admin_user
        monkeypatch.setattr(settings, "upload_max_bytes", 100)
        resp = await client.post(
            "/api/v1/admin/models/validate-manifest",
            json={"files": _files(("model.safetensors", 200))},
        )
        ws = {w["code"]: w for w in resp.json()["data"]["warnings"]}
        assert "oversize" in ws
        assert ws["oversize"]["severity"] == "fatal"
