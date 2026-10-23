"""ProxyServer 维护模式测试：快照下发维护态，管道拦租户 Key、平台直发 Key 放行。

循 test_proxy_app.py 范式（FakeSession 配置态 + FakeRedis + app.state.proxy.rebuild）。
维护拒绝走 reject 计数、不落 request_log（拒绝不落账约定）。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from _fakes import FakeSession

import llmpod.apps.proxyserver.routes.inference as app_mod
from llmpod.apps.proxyserver.app import create_proxy_app
from llmpod.apps.proxyserver.snapshot import MaintenanceState
from llmpod.models import ApiKey, ModelEndpoint, RequestLog, Route, RouteBinding, Tenant
from llmpod.services.settings_service import clear_settings_cache


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = str(value)

    async def delete(self, *keys):
        pass

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def incrby(self, key, amount):
        self.store[key] = str(int(self.store.get(key, "0")) + amount)
        return int(self.store[key])

    async def expire(self, key, seconds):
        return True


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _seeded_db() -> FakeSession:
    """配置态：路由 m1(published, 1 端点) + 平台直发 Key（tenant_id=None）+ 租户 Key。"""
    db = FakeSession()
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    db.preload(Tenant(id=5, name="租户5", status="active", created_at=now, updated_at=now))
    db.preload(
        Route(
            id=1,
            name="m1",
            status="published",
            input_price_per_mtok=None,
            output_price_per_mtok=None,
            failover_enabled=False,
            failover_max_retries=0,
            created_at=now,
            updated_at=now,
        )
    )
    db.preload(
        ModelEndpoint(
            id=10,
            deployment_id="dep-1",
            instance_id=None,
            engine_type="vllm",
            protocol="openai",
            api_base="http://engine.test",
            path="/v1/chat/completions",
            api_key=None,
            served_model="internal-m1",
            name="m1",
            context_window=None,
            operations=["generate"],
            capabilities=[],
            enabled=True,
            health_status="healthy",
            created_at=now,
            updated_at=now,
        )
    )
    db.preload(
        RouteBinding(
            id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True, created_at=now, updated_at=now
        )
    )
    db.preload(
        ApiKey(
            id=1,
            key_hash=_hash("sk-platform"),
            tenant_id=None,
            name="平台Key",
            allowed_models=["m1"],
            rpm_limit=None,
            tpm_limit=None,
            total_token_quota=None,
            model_token_quotas={},
            status="active",
            expires_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.preload(
        ApiKey(
            id=2,
            key_hash=_hash("sk-tenant"),
            tenant_id=5,
            name="租户Key",
            allowed_models=["m1"],
            rpm_limit=None,
            tpm_limit=None,
            total_token_quota=None,
            model_token_quotas={},
            status="active",
            expires_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    return db


@pytest.fixture
async def client(monkeypatch):
    app = create_proxy_app()
    db = _seeded_db()
    redis = FakeRedis()

    async def _db_gen():
        yield db

    async def _redis_gen():
        yield redis

    from llmpod.db import get_db, get_redis

    app.dependency_overrides[get_db] = _db_gen
    app.dependency_overrides[get_redis] = _redis_gen

    clear_settings_cache()
    monkeypatch.setattr(
        "llmpod.apps.proxyserver.snapshot.get_maintenance",
        _maintenance_stub(enabled=False),
    )
    await app.state.proxy.rebuild(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, db, redis, app

    app.dependency_overrides.clear()
    clear_settings_cache()


def _maintenance_stub(enabled: bool, message: str = "系统维护中"):
    async def _stub(db):
        return MaintenanceState(enabled=enabled, message=message)

    return _stub


async def _enable_maintenance(app, db, monkeypatch, message="升级维护中"):
    """切换维护态并重建快照（模拟既有轮询周期的热更新）。"""
    monkeypatch.setattr(
        "llmpod.apps.proxyserver.snapshot.get_maintenance",
        _maintenance_stub(enabled=True, message=message),
    )
    clear_settings_cache()
    await app.state.proxy.rebuild(db)


def _attach_logger(app, db, tmp_path):
    from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore
    from llmpod.apps.proxyserver.request_logger import RequestLogger

    app.state.proxy.logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))
    app.state.proxy.log_db = db


_OK_CHAT_BODY = {
    "id": "c",
    "object": "chat.completion",
    "created": 1,
    "model": "internal-m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


def _stub_openai_client(monkeypatch):
    """MockTransport 返回成功 chat completion，并把路由的 get_openai_client 换成它。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_CHAT_BODY)

    fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
    return fake_client


PLATFORM_AUTH = {"Authorization": "Bearer sk-platform"}
TENANT_AUTH = {"Authorization": "Bearer sk-tenant"}


class TestMaintenanceRejection:
    async def test_tenant_key_503(self, client, monkeypatch):
        """维护期租户 Key：503 + OpenAI 错误体 code=maintenance，message 为维护文案。"""
        c, db, redis, app = client
        await _enable_maintenance(app, db, monkeypatch)

        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []}, headers=TENANT_AUTH)
        assert resp.status_code == 503
        err = resp.json()["error"]
        assert err["type"] == "service_unavailable"
        assert err["code"] == "maintenance"
        assert err["message"] == "升级维护中"

    async def test_platform_key_allowed_under_maintenance(self, client, monkeypatch, tmp_path):
        """维护期平台直发 Key（tenant_id=None）放行，请求照常转发。"""
        c, db, redis, app = client
        await _enable_maintenance(app, db, monkeypatch)
        _attach_logger(app, db, tmp_path)
        _stub_openai_client(monkeypatch)

        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []}, headers=PLATFORM_AUTH)
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "ok"

    async def test_not_maintenance_allows_tenant(self, client, monkeypatch):
        """非维护期：租户 Key 正常进入管道（到达选路/转发层）。"""
        c, db, redis, app = client
        _stub_openai_client(monkeypatch)

        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []}, headers=TENANT_AUTH)
        assert resp.status_code == 200

    async def test_maintenance_reject_not_logged(self, client, monkeypatch, tmp_path):
        """维护拒绝不写 request_log（拒绝观测走 reject 指标，沿用拒绝不落账约定）。"""
        c, db, redis, app = client
        await _enable_maintenance(app, db, monkeypatch)
        _attach_logger(app, db, tmp_path)

        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []}, headers=TENANT_AUTH)
        assert resp.status_code == 503
        await asyncio.sleep(0.05)
        assert db.all(RequestLog) == []

    async def test_models_list_tenant_rejected(self, client, monkeypatch):
        """维护期 /v1/models 租户 Key 同样 503（与推理管道同口径）。"""
        c, db, redis, app = client
        await _enable_maintenance(app, db, monkeypatch)

        resp = await c.get("/v1/models", headers=TENANT_AUTH)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "maintenance"

    async def test_models_list_platform_allowed(self, client, monkeypatch):
        """维护期 /v1/models 平台直发 Key 放行。"""
        c, db, redis, app = client
        await _enable_maintenance(app, db, monkeypatch)

        resp = await c.get("/v1/models", headers=PLATFORM_AUTH)
        assert resp.status_code == 200
        assert resp.json()["object"] == "list"
