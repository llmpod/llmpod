"""/v1/messages 入站契约测试（Anthropic 客户端形态）。

与 OpenAI 系的两处差异单列一个文件锁定：

- **鉴权头**：Anthropic SDK 只发 ``x-api-key``，不发 ``Authorization``；
- **错误信封**：Anthropic SDK 只解析 ``{"type":"error","error":{"type","message"}}``。

stub 驱动：FakeSession 配置态 + httpx.MockTransport 假引擎（范式同 test_proxy_app.py）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import anthropic
import httpx
import httpx2
import pytest
from httpx import ASGITransport, AsyncClient
from _fakes import FakeRedis, FakeSession

import llmpod.apps.proxyserver.routes.inference as app_mod
from llmpod.apps.proxyserver.app import create_proxy_app
from llmpod.models import ApiKey, ModelEndpoint, Route, RouteBinding

CLIENT_KEY = "sk-anthropic-live"
UPSTREAM_KEY = "upstream-secret"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _endpoint(
    *,
    endpoint_id: int,
    route_name: str,
    protocol: str,
    api_key: str | None,
    now: datetime,
) -> ModelEndpoint:
    """端点桩：一协议一条记录（protocol 决定 /v1/messages 能否选到它）。"""
    return ModelEndpoint(
        id=endpoint_id,
        deployment_id=None,
        model_deployment_replica_id=None,
        instance_id=None,
        engine_type="external",
        protocol=protocol,
        api_base=f"https://{route_name}-{protocol}.test",
        path=None,
        api_key=api_key,
        served_model=f"upstream-{route_name}",
        name=f"{route_name}-{protocol}",
        provider_name="stub",
        context_window=None,
        operations=[],
        capabilities=[],
        enabled=True,
        health_status="healthy",
        last_synced_at=None,
        created_at=now,
        updated_at=now,
    )


def _seeded_db() -> FakeSession:
    """配置态：m1(published, anthropic 端点) · m2(published, 只有 openai 端点)
    · m3(unpublished)；Key 授权全部三个模型名。"""
    db = FakeSession()
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)

    for route_id, name, status in ((1, "m1", "published"), (2, "m2", "published"), (3, "m3", "unpublished")):
        db.preload(
            Route(
                id=route_id,
                name=name,
                status=status,
                origin="manual",
                model_type="llm",
                description=None,
                context_length=None,
                tags=[],
                input_price_per_mtok=None,
                output_price_per_mtok=None,
                cached_input_price_per_mtok=None,
                failover_enabled=False,
                failover_max_retries=0,
                created_at=now,
                updated_at=now,
            )
        )

    db.preload(_endpoint(endpoint_id=10, route_name="m1", protocol="anthropic", api_key=UPSTREAM_KEY, now=now))
    db.preload(_endpoint(endpoint_id=11, route_name="m2", protocol="openai", api_key=None, now=now))
    db.preload(_endpoint(endpoint_id=12, route_name="m3", protocol="anthropic", api_key=None, now=now))

    for binding_id, route_id, endpoint_id in ((1, 1, 10), (2, 2, 11), (3, 3, 12)):
        db.preload(
            RouteBinding(
                id=binding_id,
                route_id=route_id,
                endpoint_id=endpoint_id,
                weight=1,
                priority=0,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )

    db.preload(
        ApiKey(
            id=1,
            key_hash=_hash(CLIENT_KEY),
            tenant_id=None,
            name="k",
            allowed_models=["m1", "m2", "m3"],
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, db, redis, app

    app.dependency_overrides.clear()


def _sse_upstream(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


def _message_body(*, status: int = 200) -> httpx.Response:
    if status != 200:
        return httpx.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "upstream-m1",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    )


class TestInboundAuth:
    """鉴权头：x-api-key 与 Bearer 等价接受，取到明文后同一套校验。"""

    async def test_x_api_key_authenticates_and_forwards(self, client, monkeypatch):
        c, *_ = client
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["key"] = request.headers.get("x-api-key")
            seen["version"] = request.headers.get("anthropic-version")
            seen["beta"] = request.headers.get("anthropic-beta")
            seen["auth"] = request.headers.get("Authorization")
            return _message_body()

        monkeypatch.setattr(app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(handler))

        resp = await c.post(
            "/v1/messages",
            json={"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
            headers={
                "x-api-key": CLIENT_KEY,
                # 客户端自己的协商头：与适配器缺省值不同，验证是透传而非硬编码
                "anthropic-version": "2024-10-22",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
        )

        assert resp.status_code == 200
        assert resp.json()["model"] == "m1"  # 上游 served_model 回写为对外别名
        assert seen["url"] == "https://m1-anthropic.test/v1/messages"
        # 上游拿到的是端点自己的 Key，客户端 Key 不外泄
        assert seen["key"] == UPSTREAM_KEY
        assert seen["auth"] is None
        # 客户端的能力协商头原样到达上游（不是网关定死的常量）
        assert seen["version"] == "2024-10-22"
        assert seen["beta"] == "prompt-caching-2024-07-31"

    async def test_missing_version_header_falls_back_to_default(self, client, monkeypatch):
        """客户端没带 anthropic-version（老 SDK / 裸 curl）：按缺省版本补，不缺头。"""
        c, *_ = client
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["version"] = request.headers.get("anthropic-version")
            return _message_body()

        monkeypatch.setattr(app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(handler))

        resp = await c.post(
            "/v1/messages",
            json={"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": CLIENT_KEY},
        )

        assert resp.status_code == 200
        assert seen["version"] == "2023-06-01"

    async def test_both_auth_headers_stale_side_does_not_break_request(self, client, monkeypatch):
        """同时带 Bearer（有效）与陈旧 x-api-key 的请求照常过——按头形态定优先级
        会让「多带一个头」把原本能过的请求打成 401。"""
        c, *_ = client
        monkeypatch.setattr(
            app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(lambda request: _message_body())
        )

        resp = await c.post(
            "/v1/messages",
            json={"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {CLIENT_KEY}", "x-api-key": "sk-stale-from-other-env"},
        )

        assert resp.status_code == 200

    async def test_bearer_still_accepted_on_messages(self, client, monkeypatch):
        c, *_ = client
        monkeypatch.setattr(
            app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(lambda request: _message_body())
        )

        resp = await c.post(
            "/v1/messages",
            json={"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {CLIENT_KEY}"},
        )

        assert resp.status_code == 200

    async def test_missing_key_401(self, client):
        c, *_ = client
        resp = await c.post("/v1/messages", json={"model": "m1", "messages": []})
        assert resp.status_code == 401
        assert resp.json()["error"]["type"] == "authentication_error"

    async def test_unknown_key_401(self, client):
        c, *_ = client
        resp = await c.post("/v1/messages", json={"model": "m1", "messages": []}, headers={"x-api-key": "sk-nope"})
        assert resp.status_code == 401
        assert resp.json()["error"]["type"] == "authentication_error"

    async def test_lone_surrogate_in_content_is_forwarded_not_500(self, client, monkeypatch):
        """消息里带孤立代理项（JSON \\ud800 转义）不该把请求炸成裸 500——指纹
        计算在请求路径上，必须容忍坏字符。

        裸字节直发（不经 httpx 的 json= 序列化）：客户端侧严格 utf-8 编不出
        孤立代理项，只有 JSON 转义形态能把它送到网关。
        """
        c, *_ = client
        monkeypatch.setattr(
            app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(lambda request: _message_body())
        )

        resp = await c.post(
            "/v1/messages",
            content=(b'{"model":"m1","max_tokens":16,"messages":[{"role":"user","content":"bad:\\ud800end"}]}'),
            headers={"x-api-key": CLIENT_KEY, "content-type": "application/json"},
        )

        assert resp.status_code == 200
        assert resp.json()["model"] == "m1"


class TestAnthropicErrorEnvelope:
    """拒绝与上游失败的响应体必须是 Anthropic 信封，而非 OpenAI 形状。"""

    @staticmethod
    def _envelope(resp) -> dict:
        body = resp.json()
        assert "error" in body, f"缺 Anthropic 错误信封: {body}"
        assert "code" not in body["error"], f"Anthropic 无 code 字段: {body}"
        return body

    async def test_model_not_found_404(self, client):
        c, *_ = client
        resp = await c.post("/v1/messages", json={"model": "nope", "messages": []}, headers={"x-api-key": CLIENT_KEY})
        assert resp.status_code == 404
        body = self._envelope(resp)
        assert body["type"] == "error"
        assert body["error"]["type"] == "not_found_error"

    async def test_unpublished_model_403(self, client):
        c, *_ = client
        resp = await c.post("/v1/messages", json={"model": "m3", "messages": []}, headers={"x-api-key": CLIENT_KEY})
        assert resp.status_code == 403
        assert self._envelope(resp)["error"]["type"] == "permission_error"

    async def test_no_anthropic_endpoint_503(self, client):
        """m2 只有 openai 端点：协议内过滤后无候选（不做协议转换）。"""
        c, *_ = client
        resp = await c.post("/v1/messages", json={"model": "m2", "messages": []}, headers={"x-api-key": CLIENT_KEY})
        assert resp.status_code == 503
        assert self._envelope(resp)["error"]["type"] == "api_error"

    async def test_upstream_500_maps_to_502(self, client, monkeypatch):
        c, *_ = client
        monkeypatch.setattr(
            app_mod,
            "get_anthropic_client",
            lambda *a, **k: _sse_upstream(lambda request: _message_body(status=500)),
        )
        resp = await c.post(
            "/v1/messages",
            json={"model": "m1", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": CLIENT_KEY},
        )
        assert resp.status_code == 502
        assert self._envelope(resp)["error"]["type"] == "api_error"

    async def test_malformed_json_400(self, client):
        c, *_ = client
        resp = await c.post(
            "/v1/messages",
            content=b"{not json",
            headers={"x-api-key": CLIENT_KEY, "content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert self._envelope(resp)["error"]["type"] == "invalid_request_error"


class TestOpenAIEnvelopeUnchanged:
    """OpenAI 系路由不受影响：仍出 OpenAI 形状（带 code），且不认 x-api-key 之外的形态变化。"""

    async def test_chat_401_openai_body(self, client):
        c, *_ = client
        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []})
        assert resp.status_code == 401
        err = resp.json()["error"]
        assert err["type"] == "invalid_request_error"
        assert err["code"] == "invalid_api_key"

    async def test_chat_404_openai_body(self, client):
        c, *_ = client
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "nope", "messages": []},
            headers={"Authorization": f"Bearer {CLIENT_KEY}"},
        )
        assert resp.status_code == 404
        err = resp.json()["error"]
        assert err["type"] == "invalid_request_error"
        assert err["code"] == "model_not_found"


def _sdk_client(app, *, api_key: str = CLIENT_KEY) -> anthropic.AsyncAnthropic:
    """官方 Anthropic SDK 打到 API 网关 ASGI（进程内，无网络）。

    anthropic 1.0.0 拒绝注入标准 httpx.AsyncClient，要求其自带的 vendored httpx2，
    故这里用 httpx2 的 ASGITransport（与 test_official_sdk_contract 同款约束）。
    """
    return anthropic.AsyncAnthropic(
        base_url="http://test",
        api_key=api_key,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), timeout=10),
    )


class TestOfficialAnthropicSdk:
    """用真 SDK 复现报障路径：官方客户端 + 面板新建的 Key 打 /v1/messages。

    前面那些用例用裸 httpx 手工拼 x-api-key，只能证明「我们的实现按预期解析」；
    这里换成官方 SDK，证明「真实客户端确实能连上」——两条证据链不同。
    """

    async def test_sdk_call_succeeds(self, client, monkeypatch):
        _c, _db, _redis, app = client
        monkeypatch.setattr(
            app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(lambda request: _message_body())
        )

        message = await _sdk_client(app).messages.create(
            model="m1", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
        )

        assert message.model == "m1"  # 上游 served_model 回写为对外别名
        assert message.content[0].text == "ok"
        assert message.stop_reason == "end_turn"

    async def test_sdk_streaming_call_succeeds(self, client, monkeypatch):
        _c, _db, _redis, app = client
        events = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant",'
            '"model":"upstream-m1","content":[],"stop_reason":null,"usage":{"input_tokens":3,"output_tokens":1}}}\n\n'
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
            'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        monkeypatch.setattr(
            app_mod,
            "get_anthropic_client",
            lambda *a, **k: _sse_upstream(
                lambda request: httpx.Response(200, text=events, headers={"content-type": "text/event-stream"})
            ),
        )

        seen_models: list[str] = []
        async with _sdk_client(app).messages.stream(
            model="m1", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
        ) as stream:
            async for text in stream.text_stream:
                seen_models.append(text)

        assert seen_models == ["ok"]

    async def test_sdk_reports_unknown_key_as_authentication_error(self, client):
        """SDK 必须能把 401 解析成 AuthenticationError 并带上我们的 message。

        错误体若还是 OpenAI 形状，SDK 解析不出 message——报错会退化成无信息量
        的通用异常，这正是报障时「提示 KEY 不存在」说不清来源的原因之一。
        """
        _c, _db, _redis, app = client

        with pytest.raises(anthropic.AuthenticationError) as exc:
            await _sdk_client(app, api_key="sk-not-a-key").messages.create(
                model="m1", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
            )

        assert exc.value.status_code == 401
        assert "invalid api key" in str(exc.value)

    async def test_sdk_reports_missing_model_as_not_found(self, client):
        _c, _db, _redis, app = client

        with pytest.raises(anthropic.NotFoundError) as exc:
            await _sdk_client(app).messages.create(
                model="nope", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
            )

        assert "not found" in str(exc.value)

    async def test_sdk_count_tokens_roundtrip(self, client, monkeypatch):
        """SDK 一等公民方法 /v1/messages/count_tokens 要通：Claude Code 用它做
        上下文 / 自动压缩判断，404 会让客户端把「网关没实现」当成「模型不存在」。"""
        _c, _db, _redis, app = client
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["model"] = json.loads(request.content).get("model")
            return httpx.Response(200, json={"input_tokens": 42})

        monkeypatch.setattr(app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(handler))

        result = await _sdk_client(app).messages.count_tokens(model="m1", messages=[{"role": "user", "content": "hi"}])

        assert result.input_tokens == 42
        assert seen["url"] == "https://m1-anthropic.test/v1/messages/count_tokens"
        assert seen["model"] == "upstream-m1"  # 别名换上游真名

    async def test_sdk_exposes_request_id_header_for_troubleshooting(self, client, monkeypatch):
        """真 SDK 拿原始响应头验 Anthropic 惯例头名：报障时用户被 SDK 提示抓
        ``request-id``（官方无 x- 前缀），网关在 anthropic 协议上并排给两个头、
        值同源，用户按任一习惯都能拿到可反查计量行的号。"""
        _c, _db, _redis, app = client
        monkeypatch.setattr(
            app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(lambda request: _message_body())
        )

        raw = await _sdk_client(app).messages.with_raw_response.create(
            model="m1", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
        )

        assert raw.headers.get("request-id")
        assert raw.headers.get("request-id") == raw.headers.get("x-request-id")

    async def test_count_tokens_targets_subpath_of_registered_path(self, client, monkeypatch):
        """端点登记了 path（本地部署形态：api_base 带版本根 /v1、path="messages"，
        由 category_to_path 派生）时，count_tokens 必须落在主端点 path 之下——URL
        解析只认登记 path、忽略传入后缀，漏了这层就会打到 /v1/messages 真跑一次
        生成（且 usage=None 不落账，白烧的算力不留痕）。"""
        _c, db, _redis, app = client
        for ep in db.all(ModelEndpoint):
            if ep.protocol == "anthropic":
                ep.api_base = "http://10.0.0.5:8000/v1"
                ep.path = "messages"
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"input_tokens": 42})

        monkeypatch.setattr(app_mod, "get_anthropic_client", lambda *a, **k: _sse_upstream(handler))

        result = await _sdk_client(app).messages.count_tokens(model="m1", messages=[{"role": "user", "content": "hi"}])

        assert result.input_tokens == 42
        assert seen["url"] == "http://10.0.0.5:8000/v1/messages/count_tokens"

    async def test_count_tokens_unknown_model_is_not_found_envelope(self, client):
        """count_tokens 打不存在的模型：走准入 404，出 Anthropic 信封（SDK 能解析）。"""
        c, *_ = client
        resp = await c.post(
            "/v1/messages/count_tokens",
            json={"model": "nope", "messages": []},
            headers={"x-api-key": CLIENT_KEY},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "not_found_error"
