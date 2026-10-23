"""分发端点 HTTP 单测：令牌鉴权 + manifest + 文件 Range（TestClient，DB 存根）。"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import llmpod.apps.webserver.routes.public.distribution as dist
import llmpod.config.settings as settings_mod
from llmpod.apps.webserver.errors import register_exception_handlers
from llmpod.apps.webserver.routes.public.distribution import router as distribution_router
from llmpod.apps.webserver.utils.download_token import mint_download_token, download_signing_key
from llmpod.models.model import Model


class _FakeSession:
    def __init__(self, model):
        self._model = model

    async def get(self, cls, pk):
        if cls is Model and pk == self._model.id:
            return self._model
        return None


@pytest.fixture
def file_service_app(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    mdir = store / "m1"
    mdir.mkdir()
    (mdir / "config.json").write_text("{}")
    (mdir / "w.safetensors").write_bytes(b"0123456789")  # 10 bytes

    monkeypatch.setattr(settings_mod.settings, "secret_key", "test-deployment-secret")
    monkeypatch.setattr(settings_mod.settings, "model_store_dir", str(store))
    monkeypatch.setattr(settings_mod.settings, "download_token_max_ttl", 7200)
    monkeypatch.setattr(settings_mod.settings, "download_manifest_cache_ttl", 60)

    model = Model(name="m1", model_type="llm", source="local", source_url=str(mdir))
    model.id = 42

    @asynccontextmanager
    async def fake_db():
        yield _FakeSession(model)

    monkeypatch.setattr(dist, "get_db_context", fake_db)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(distribution_router, prefix="/api/v1/distribution")
    return TestClient(app), model


def test_manifest_requires_valid_token(file_service_app):
    client, model = file_service_app
    r = client.get("/api/v1/distribution/manifest", params={"model_id": model.id, "token": "bad"})
    assert r.status_code in (401, 403)


def test_shared_node_token_cannot_mint_model_download_token(file_service_app):
    client, model = file_service_app
    forged, _ = mint_download_token("cluster-key", model.id, 600)
    response = client.get("/api/v1/distribution/manifest", params={"model_id": model.id, "token": forged})
    assert response.status_code == 401


def test_manifest_returns_file_list(file_service_app):
    client, model = file_service_app
    tok, _ = mint_download_token(download_signing_key(settings_mod.settings), model.id, 600)
    r = client.get("/api/v1/distribution/manifest", params={"model_id": model.id, "token": tok})
    assert r.status_code == 200
    data = r.json()
    assert {f["path"] for f in data["files"]} == {"config.json", "w.safetensors"}
    assert data["total_bytes"] == 12  # 2 + 10


def test_file_full_and_range(file_service_app):
    client, model = file_service_app
    tok, _ = mint_download_token(download_signing_key(settings_mod.settings), model.id, 600)

    r = client.get("/api/v1/distribution/file", params={"model_id": model.id, "path": "w.safetensors", "token": tok})
    assert r.status_code == 200
    assert r.content == b"0123456789"

    # Range 续传（从 offset 4 起）
    r2 = client.get(
        "/api/v1/distribution/file",
        params={"model_id": model.id, "path": "w.safetensors", "token": tok},
        headers={"Range": "bytes=4-"},
    )
    assert r2.status_code == 206
    assert r2.content == b"456789"
    assert r2.headers.get("content-range") == "bytes 4-9/10"


def test_file_traversal_rejected(file_service_app):
    """path 穿越（../../etc/hosts）必须被 safe_resolve 挡掉。"""
    client, model = file_service_app
    tok, _ = mint_download_token(download_signing_key(settings_mod.settings), model.id, 600)
    r = client.get(
        "/api/v1/distribution/file", params={"model_id": model.id, "path": "../../../etc/hosts", "token": tok}
    )
    assert r.status_code in (404, 400)


@pytest.fixture
def single_file_app(tmp_path, monkeypatch):
    """单文件 source_url（直指一个 .gguf）：manifest 单文件清单 + file 端点只下发根文件。"""
    store = tmp_path / "store"
    store.mkdir()
    gguf = store / "model.gguf"
    gguf.write_bytes(b"0123456789")  # 10 bytes

    monkeypatch.setattr(settings_mod.settings, "secret_key", "test-deployment-secret")
    monkeypatch.setattr(settings_mod.settings, "model_store_dir", str(store))
    monkeypatch.setattr(settings_mod.settings, "download_token_max_ttl", 7200)
    monkeypatch.setattr(settings_mod.settings, "download_manifest_cache_ttl", 60)

    model = Model(name="m2", model_type="llm", source="local", source_url=str(gguf))
    model.id = 43

    @asynccontextmanager
    async def fake_db():
        yield _FakeSession(model)

    monkeypatch.setattr(dist, "get_db_context", fake_db)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(distribution_router, prefix="/api/v1/distribution")
    return TestClient(app), model


def test_single_file_source_manifest_not_404(single_file_app):
    """单文件 source_url 的 manifest 不再 404——返回单文件清单（node 下载依据）。"""
    client, model = single_file_app
    tok, _ = mint_download_token(download_signing_key(settings_mod.settings), model.id, 600)
    r = client.get("/api/v1/distribution/manifest", params={"model_id": model.id, "token": tok})
    assert r.status_code == 200, f"单文件源 manifest 不应 404: {r.text[:200]}"
    data = r.json()
    assert [f["path"] for f in data["files"]] == ["model.gguf"]
    assert data["total_bytes"] == 10


def test_single_file_source_file_endpoint(single_file_app):
    """单文件源 file 端点：path=根文件名 → 200（Range 续传可用）；其余 path 404。"""
    client, model = single_file_app
    tok, _ = mint_download_token(download_signing_key(settings_mod.settings), model.id, 600)
    r = client.get("/api/v1/distribution/file", params={"model_id": model.id, "path": "model.gguf", "token": tok})
    assert r.status_code == 200
    assert r.content == b"0123456789"

    r2 = client.get("/api/v1/distribution/file", params={"model_id": model.id, "path": "neighbor.bin", "token": tok})
    assert r2.status_code == 404, "单文件源只暴露根文件，邻文件不可借此读取"
