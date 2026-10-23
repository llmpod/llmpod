"""管理端「在线调试」转发路由集成测试（/api/v1/admin/inference/*，stub 驱动）。

覆盖 routes/admin/inference.py：
- 非流式 POST：平台 Key 注入 Authorization、URL 拼接、Content-Type 透传、
  逐跳响应头（content-length 等）不透传；
- SSE 流式：字节级原样回传，BackgroundTask 关闭上游响应与客户端；
- 上游非 2xx 透传（429）；网关不可达 → 502 JSON；GET 空 body 不带 Content-Type。

网关用 httpx.MockTransport 桩（不连真网络）；ensure_active_platform_key 打桩
返回带 plaintext 的替身（避免触 PG）；get_db 由 FakeSession 承接 commit。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from httpx import AsyncClient

from llmpod.app import app
from llmpod.db import get_db

from _fakes import FakeSession, admin_session

BASE = "/api/v1/admin/inference"

STUB_KEY = SimpleNamespace(plaintext="sk-stub-plaintext")

# 模块导入期捕获真类：测试体内 httpx.AsyncClient 会被 _install_transport 换桩
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
async def session():
    s = FakeSession()

    async def _override():
        yield s

    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[get_current_user] = lambda: admin_session()
    yield s
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _stub_platform_key(monkeypatch):
    async def _ensure(db, *, name):
        return STUB_KEY

    monkeypatch.setattr("llmpod.apps.webserver.routes.admin.inference.ensure_active_platform_key", _ensure)


def _install_transport(monkeypatch, handler):
    """把路由内自建的 httpx.AsyncClient 换成挂 MockTransport 的客户端（不连真网络）。"""
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient  # 先捕真类，避免工厂内解析到补丁自身（递归）

    def _client(*_args, **_kwargs):
        return real_cls(transport=transport)

    monkeypatch.setattr("llmpod.apps.webserver.routes.admin.inference.httpx.AsyncClient", _client)


class _ChunkStream(httpx.AsyncByteStream):
    """显式异步字节流：MockTransport 响应若用具体 content 构造会被 httpx 预读
    （is_stream_consumed=True），转发层 aiter_raw 直接 StreamConsumed；显式
    stream= 才能模拟可流式读的上游。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _upstream_response(
    *, status: int = 200, chunks: list[bytes] | None = None, headers: dict | None = None, json_body: dict | None = None
) -> httpx.Response:
    """构造可流式读的上游响应（json_body 时单块字节流）。"""
    if json_body is not None:
        import json as _json

        chunks = [_json.dumps(json_body).encode()]
    return httpx.Response(status, stream=_ChunkStream(chunks or []), headers=headers or {})


from llmpod.apps.webserver.auth.deps import get_current_user  # noqa: E402 — fixture 定义后才 import，保持就近


class TestForwardNonStream:
    async def test_injects_platform_key_and_builds_url(self, client: AsyncClient, session, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["content_type"] = request.headers.get("content-type")
            captured["body"] = request.read()
            # 上游自报 content-length=999：转发层必须丢弃（真实长度由传输层重算）
            return _upstream_response(
                json_body={"choices": []}, headers={"x-request-id": "req-1", "content-length": "999"}
            )

        _install_transport(monkeypatch, handler)
        # 锁定未配内部地址：本机 .env 若配过该键，回环断言会无故变红
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "proxy_internal_url", "")
        resp = await client.post(
            f"{BASE}/chat/completions",
            json={"model": "m1", "messages": []},
        )
        assert resp.status_code == 200
        assert resp.json() == {"choices": []}
        # 未配内部地址 → 回环 + proxy_port（dev 两进程同宿主）
        assert captured["url"] == "http://127.0.0.1:8090/v1/chat/completions"
        assert captured["auth"] == f"Bearer {STUB_KEY.plaintext}"  # 平台 Key 注入
        assert captured["content_type"] == "application/json"
        assert b"m1" in captured["body"]
        # 业务响应头透传
        assert resp.headers["x-request-id"] == "req-1"
        # 上游 content-length 不透传（真实长度由本层传输重算）
        assert resp.headers.get("content-length") != "999"
        # key 确保逻辑后提交事务
        assert session._staged == []

    async def test_internal_url_overrides_loopback(self, client: AsyncClient, session, monkeypatch):
        """配了 LLMPOD_PROXY_INTERNAL_URL → 走它（容器部署用网关服务名即可访问）。"""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return _upstream_response(json_body={"choices": []})

        _install_transport(monkeypatch, handler)
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "proxy_internal_url", "http://proxyserver:8090/")
        resp = await client.post(f"{BASE}/chat/completions", json={"model": "m1", "messages": []})
        assert resp.status_code == 200
        # 尾斜杠去掉，不拼出 //v1
        assert captured["url"] == "http://proxyserver:8090/v1/chat/completions"

    async def test_malformed_internal_url_returns_502_not_500(self, client, session, monkeypatch):
        """内部地址端口非法（build_request 抛 InvalidURL，非 HTTPError 子类）→ 收进 502。"""
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "proxy_internal_url", "http://proxyserver:not-a-port")
        resp = await client.post(f"{BASE}/chat/completions", json={"model": "m1", "messages": []})
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "bad_gateway"

    async def test_get_without_body_has_no_content_type(self, client: AsyncClient, session, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["has_ct"] = "content-type" in request.headers
            captured["method"] = request.method
            return _upstream_response(json_body={"models": []})

        _install_transport(monkeypatch, handler)
        resp = await client.get(f"{BASE}/models")
        assert resp.status_code == 200
        assert captured["method"] == "GET"
        assert captured["url"].endswith("/v1/models")
        assert captured["has_ct"] is False  # 空 body 不注入 Content-Type

    async def test_upstream_error_status_passthrough(self, client: AsyncClient, session, monkeypatch):
        def handler(_request: httpx.Request) -> httpx.Response:
            return _upstream_response(status=429, json_body={"error": {"message": "rate limited"}})

        _install_transport(monkeypatch, handler)
        resp = await client.post(f"{BASE}/chat/completions", json={})
        assert resp.status_code == 429
        assert resp.json()["error"]["message"] == "rate limited"


class TestForwardStream:
    async def test_sse_stream_passthrough_and_background_close(self, client: AsyncClient, session, monkeypatch):
        chunks = [b'data: {"id":"1"}\n\n', b"data: [DONE]\n\n"]
        closed = {"upstream": False}

        def handler(_request: httpx.Request) -> httpx.Response:
            return _upstream_response(chunks=chunks, headers={"content-type": "text/event-stream"})

        _install_transport(monkeypatch, handler)
        # 观察 BackgroundTask 资源回收：包住上游响应的 aclose
        real_send = _REAL_ASYNC_CLIENT.send

        async def _send(self, req, **kw):
            upstream = await real_send(self, req, **kw)
            orig_aclose = upstream.aclose

            async def _aclose():
                closed["upstream"] = True
                await orig_aclose()

            upstream.aclose = _aclose  # type: ignore[method-assign]
            return upstream

        monkeypatch.setattr(_REAL_ASYNC_CLIENT, "send", _send)

        resp = await client.post(f"{BASE}/chat/completions", json={"stream": True})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.text == 'data: {"id":"1"}\n\ndata: [DONE]\n\n'  # 字节级原样回传
        assert closed["upstream"] is True  # BackgroundTask 已关上游响应（_close_client）


class TestForwardUnavailable:
    async def test_api_gateway_unreachable_502(self, client: AsyncClient, session, monkeypatch):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=_request)

        _install_transport(monkeypatch, handler)
        resp = await client.post(f"{BASE}/chat/completions", json={})
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["message"] == "API gateway unreachable"
        assert body["error"]["code"] == "bad_gateway"

    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.post(f"{BASE}/chat/completions", json={})
        assert resp.status_code == 401
