"""模型上传 + local inspect 集成测试（stub 驱动，不依赖真实 PG/Redis）。

验证：
- ``POST /admin/models/upload`` 路由注册 + 受 ``require_permission`` 保护（无登录态→401）；
- 上传 multipart 文件落地到 ``settings.model_store_dir`` 下，返回 ``Result[UploadResult]``
  含 staged 路径与文件清单；
- ``POST /admin/models/inspect {source:local, source_url:<staged 路径>}`` 经 manager 本地直读
  返回 ``ModelFiles``（architecture/format/size_gb 已填），不再需要 node 在线。
"""

from __future__ import annotations

import json

from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.config.settings import settings


def _admin_user() -> SessionData:
    return admin_session()


class TestUploadEndpoint:
    async def test_upload_no_auth_returns_401(self, client: AsyncClient):
        # 未覆盖 get_current_user → require_permission 拒绝
        resp = await client.post("/api/v1/admin/models/upload")
        assert resp.status_code == 401

    async def test_upload_empty_body_rejected(self, client: AsyncClient, monkeypatch, tmp_path):
        app.dependency_overrides[get_current_user] = _admin_user
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))
        # 未带 multipart body → FastAPI 校验缺失必填项 → 422
        resp = await client.post("/api/v1/admin/models/upload")
        assert resp.status_code == 422

    async def test_upload_lands_and_inspects_local(self, client: AsyncClient, monkeypatch, tmp_path):
        """上传 config.json + model.safetensors → 拿路径 → inspect source=local 预填。"""
        app.dependency_overrides[get_current_user] = _admin_user
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))

        config = {"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"}
        resp = await client.post(
            "/api/v1/admin/models/upload?name=qwen-test",
            files=[
                ("files", ("config.json", json.dumps(config).encode(), "application/json")),
                ("files", ("model.safetensors", b"\x00" * 100, "application/octet-stream")),
            ],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        staged_path = data["path"]
        assert "qwen-test" in staged_path
        assert set(data["files"]) == {"config.json", "model.safetensors"}
        # 文件确实落地
        import os

        assert os.path.exists(os.path.join(staged_path, "config.json"))
        assert os.path.exists(os.path.join(staged_path, "model.safetensors"))

        # inspect source=local（manager 本地直读，无需 node）
        resp2 = await client.post(
            "/api/v1/admin/models/inspect",
            json={"source": "local", "source_url": staged_path},
        )
        assert resp2.status_code == 200, resp2.text
        body = resp2.json()["data"]
        # 响应体升级为 InspectResponse{files, feasibility}
        assert set(body.keys()) >= {"files", "feasibility"}
        mf = body["files"]
        assert mf["success"] is True
        assert mf["structure"] == "directory"
        assert mf["layout"] == "transformers"
        assert mf["format"] == "safetensors"
        assert mf["architecture"] == "Qwen2ForCausalLM"
        assert mf["suggested_type"] is None  # local 无 pipeline_tag，用户选
        # 可行性检测字段存在（feasibility=true 默认开）；stub DB 无 execute →
        # best-effort 吞错后 feasibility=None（happy path 由 test_feasibility 覆盖）
        assert "feasibility" in body

    async def test_upload_filename_traversal_sanitized(self, client: AsyncClient, monkeypatch, tmp_path):
        """文件名含路径穿越成分时仅取末段，不越出 staged 目录。"""
        app.dependency_overrides[get_current_user] = _admin_user
        monkeypatch.setattr(settings, "model_store_dir", str(tmp_path))

        resp = await client.post(
            "/api/v1/admin/models/upload?name=trav",
            files=[("files", ("../../etc/evil.bin", b"x", "application/octet-stream"))],
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["files"] == ["evil.bin"]
        import os

        assert os.path.exists(os.path.join(data["path"], "evil.bin"))
        # 没有逃逸到 tmp_path 之外
        assert not os.path.exists(os.path.join(str(tmp_path), "etc", "evil.bin"))


class TestInspectOverride:
    """用户覆盖（size_gb / model_type / model_format）优先于探测值，并反映在回显 + feasibility 入参。

    stub 探测器返回固定 ModelFiles，隔离验证「覆盖优先 + 回显反映覆盖」契约；
    feasibility 真值判定由 test_feasibility 单测覆盖（引擎早已接受可空 size/type/format）。
    """

    @staticmethod
    def _patch_inspector(monkeypatch, detected) -> None:
        from llmpod.apps.webserver.routes.admin import model as model_route

        class _FakeInspector:
            async def inspect(self, *, source_url, revision):
                return detected

        monkeypatch.setattr(model_route, "get_inspector", lambda source: _FakeInspector())

    async def test_override_wins_over_detected(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.manager.model_source import ModelFiles

        app.dependency_overrides[get_current_user] = _admin_user
        # 探测值：1.0GB / llm / safetensors；下面用覆盖值全部推翻
        detected = ModelFiles(success=True, size_gb=1.0, suggested_type="llm", format="safetensors")
        self._patch_inspector(monkeypatch, detected)

        resp = await client.post(
            "/api/v1/admin/models/inspect",
            json={
                "source": "local",
                "source_url": "/tmp/x",
                "size_gb": 70.0,
                "model_type": "embedding",
                "model_format": "gguf",
            },
        )
        assert resp.status_code == 200, resp.text
        mf = resp.json()["data"]["files"]
        assert mf["size_gb"] == 70.0  # 覆盖优先于探测的 1.0（解锁原本因 size=None 恒 False 的 deployable）
        assert mf["suggested_type"] == "embedding"  # 覆盖优先于探测的 llm
        assert mf["format"] == "gguf"  # 覆盖优先于探测的 safetensors（切到 GGUF 分支）

    async def test_no_override_passes_detected_through(self, client: AsyncClient, monkeypatch):
        from llmpod.apps.manager.model_source import ModelFiles

        app.dependency_overrides[get_current_user] = _admin_user
        detected = ModelFiles(success=True, size_gb=1.0, suggested_type="llm", format="safetensors")
        self._patch_inspector(monkeypatch, detected)

        # 不传任何覆盖 → 探测值原样透传（回归）
        resp = await client.post(
            "/api/v1/admin/models/inspect",
            json={"source": "local", "source_url": "/tmp/x"},
        )
        assert resp.status_code == 200, resp.text
        mf = resp.json()["data"]["files"]
        assert mf["size_gb"] == 1.0
        assert mf["suggested_type"] == "llm"
        assert mf["format"] == "safetensors"
