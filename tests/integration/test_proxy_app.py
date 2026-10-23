"""ProxyServer /v1/* 路由契约测试（stub 驱动：FakeSession 配置态 + 协议层桩）。

HTTP 层验证：管道依赖链（401/403/404/503 → OpenAI 标准错误体）、
/v1/models 列表格式、转发成功路径（非流式 JSON / 流式 SSE）、
usage 记账回调触发（限流/配额回填）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from _fakes import FakeSession

import llmpod.apps.proxyserver.routes.inference as app_mod
from llmpod.apps.proxyserver.app import create_proxy_app
from llmpod.models import ApiKey, ModelEndpoint, RequestLog, Route, RouteBinding


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = str(value)

    async def delete(self, *keys):
        self.deleted.extend(keys)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def incrby(self, key, amount):
        self.store[key] = str(int(self.store.get(key, "0")) + amount)
        return int(self.store[key])

    async def expire(self, key, seconds):
        return True


def _hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


def _seeded_db() -> FakeSession:
    """配置态：路由 m1(published, 1 端点) + m2(unpublished) + Key sk-live123。"""
    db = FakeSession()
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
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
        Route(
            id=2,
            name="m2",
            status="unpublished",
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
            key_hash=_hash("sk-live123"),
            tenant_id=None,
            name="k",
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, db, redis, app

    app.dependency_overrides.clear()


AUTH = {"Authorization": "Bearer sk-live123"}


def _fake_upstream_ok(fail_hosts: tuple[str, ...] = ()):
    """构造 mock 上游：正常返回 chat completion；host 命中 fail_hosts 的返回 500。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in fail_hosts:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "internal-m1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    return handler


def _attach_logger(app, db, tmp_path):
    """接线 fire-and-forget 记账到测试 db + tmp_path 块存储。"""
    from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore
    from llmpod.apps.proxyserver.request_logger import RequestLogger

    app.state.proxy.logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))
    app.state.proxy.log_db = db


def _add_failover_endpoint(db):
    """给路由 m1 追加高优先级端点（独立部署 dep-2，同部署同协议唯一约束不允许第二条）并开 failover。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    route = db.all(Route)[0]
    route.failover_enabled = True
    route.failover_max_retries = 1
    db.preload(
        ModelEndpoint(
            id=11,
            deployment_id="dep-2",
            instance_id=None,
            engine_type="vllm",
            protocol="openai",
            api_base="http://engine-fail.test",
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
            id=2, route_id=route.id, endpoint_id=11, weight=1, priority=5, enabled=True, created_at=now, updated_at=now
        )
    )


class TestRequestIdHeader:
    """所有推理出口统一带 x-request-id：客户端贴号报障，平台按号反查计量/流水/冻结单。

    拒绝路径（401/400）不落 request_log，响应头上的号是客户端与访问日志之间
    唯一的对账线索，必须覆盖；号在进入管道前生成，连「请求体非法即拒」也带。
    """

    async def test_nonstream_success_header_matches_logged_request_id(self, client, monkeypatch, tmp_path):
        c, db, redis, app = client
        _attach_logger(app, db, tmp_path)

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream_ok()), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        resp = await c.post(
            "/v1/chat/completions", json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]}, headers=AUTH
        )
        assert resp.status_code == 200
        request_id = resp.headers["x-request-id"]
        assert request_id  # 32 位 hex
        # OpenAI 系只跟 OpenAI 惯例头（无前缀的 request-id 是 Anthropic 惯例，不无差别双发）
        assert "request-id" not in resp.headers
        # 号跨层一致：响应头 = 计量行（对账链路的起点与终点闭合）
        await asyncio.sleep(0.05)
        assert [row.request_id for row in db.all(RequestLog)] == [request_id]

    async def test_stream_response_carries_header(self, client, monkeypatch):
        c, db, redis, app = client

        body = (
            b'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"internal-m1",'
            b'"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
            b"data: [DONE]\n\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": [], "stream": True}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.headers["x-request-id"]

    async def test_rejected_requests_still_carry_header(self, client):
        c, *_ = client
        # 401（未知 Key）：无鉴权可刷的拒绝路径，客户端手里只有这个号
        resp = await c.post(
            "/v1/chat/completions", json={"model": "m1", "messages": []}, headers={"Authorization": "Bearer sk-nope"}
        )
        assert resp.status_code == 401
        assert resp.headers["x-request-id"]

    async def test_invalid_body_400_carries_header(self, client):
        """请求体非法即拒发生在管道最前面，验证号是入口处就生成、人人有份。"""
        c, *_ = client
        resp = await c.post(
            "/v1/chat/completions", content=b"not-json", headers={"content-type": "application/json", **AUTH}
        )
        assert resp.status_code == 400
        assert resp.headers["x-request-id"]

    async def test_body_over_cap_413_before_auth(self, client, monkeypatch):
        """请求体超上限在读 body 阶段即拒（413），先于鉴权——匿名大 body 不再
        被全量缓冲。Content-Length 路径与 chunked 路径都要拦。"""
        from llmpod.config.settings import settings

        c, *_ = client
        monkeypatch.setattr(settings, "proxy_max_body_bytes", 1024)
        big = b"x" * 2048

        # 带 Content-Length（httpx content= 自动带）
        resp = await c.post("/v1/chat/completions", content=big, headers={"content-type": "application/json"})
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "body_too_large"
        assert resp.headers["x-request-id"]

        # chunked（无 Content-Length）：流式累计上限兜底
        async def gen():
            yield b"x" * 600
            yield b"x" * 600

        resp = await c.post("/v1/chat/completions", content=gen(), headers={"content-type": "application/json"})
        assert resp.status_code == 413

    async def test_header_ids_are_unique_per_request(self, client, monkeypatch):
        c, db, redis, app = client
        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream_ok()), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        ids = set()
        for _ in range(2):
            resp = await c.post(
                "/v1/chat/completions",
                json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                headers=AUTH,
            )
            assert resp.status_code == 200
            ids.add(resp.headers["x-request-id"])
        assert len(ids) == 2


class TestPipelineErrors:
    async def test_missing_auth_401_openai_error_body(self, client):
        c, *_ = client
        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []})
        assert resp.status_code == 401
        err = resp.json()["error"]
        assert err["type"] == "invalid_request_error"
        assert err["code"] == "invalid_api_key"

    async def test_unknown_key_401(self, client):
        c, *_ = client
        resp = await c.post(
            "/v1/chat/completions", json={"model": "m1", "messages": []}, headers={"Authorization": "Bearer sk-nope"}
        )
        assert resp.status_code == 401

    async def test_unknown_key_401_not_logged(self, client, monkeypatch, tmp_path):
        """401 拒绝不写 request_log——无鉴权请求可被外部无限刷，落库等于免费写放大通道；
        缺模型/Key/用量字段的行进调用日志列表只会污染数据。拒绝观测走指标。"""
        c, db, redis, app = client
        _attach_logger(app, db, tmp_path)
        resp = await c.post(
            "/v1/chat/completions", json={"model": "m1", "messages": []}, headers={"Authorization": "Bearer sk-nope"}
        )
        assert resp.status_code == 401
        await asyncio.sleep(0.05)
        assert db.all(RequestLog) == []

    async def test_model_not_found_404_not_logged(self, client, monkeypatch, tmp_path):
        """404 拒绝同样不写 request_log（选路前拒绝统一只打指标）。"""
        c, db, redis, app = client
        _attach_logger(app, db, tmp_path)
        resp = await c.post("/v1/chat/completions", json={"model": "nope", "messages": []}, headers=AUTH)
        assert resp.status_code == 404
        await asyncio.sleep(0.05)
        assert db.all(RequestLog) == []

    async def test_model_not_allowed_403(self, client):
        c, *_ = client
        # Key 只允许 m1；请求不存在的 model → 404；请求存在但未授权的 published
        # 别名场景需第二个 published 别名——用 m2(unpublished) 验证 403 优先级：
        # m2 未授权且未发布 → 授权检查先命中（allowed_models 优先于发布态）
        resp = await c.post("/v1/chat/completions", json={"model": "m2", "messages": []}, headers=AUTH)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "model_not_allowed"

    async def test_unknown_model_404(self, client):
        c, *_ = client
        resp = await c.post("/v1/chat/completions", json={"model": "ghost", "messages": []}, headers=AUTH)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "model_not_found"


class TestForwarding:
    async def test_chat_completions_nonstream(self, client, monkeypatch):
        c, db, redis, app = client
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "internal-m1",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            )

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "m1"  # 整形回写别名
        assert seen["body"]["model"] == "internal-m1"  # 引擎侧内部名
        assert data["usage"]["total_tokens"] == 5
        # usage 记账：TPM/配额计数器已回填
        assert redis.store.get("llmpod:quota:key:1") == "5"

    async def test_chat_completions_stream(self, client, monkeypatch):
        c, db, redis, app = client

        def handler(request: httpx.Request) -> httpx.Response:
            chunks = [
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-m1",
                    "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
                },
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-m1",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ]
            body = b"".join(b"data: " + json.dumps(x).encode() + b"\n\n" for x in chunks) + b"data: [DONE]\n\n"
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [],
                "stream": True,
            },
            headers=AUTH,
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in resp.text.split("\n\n") if ln]
        first = json.loads(lines[0][len("data: ") :])
        assert first["model"] == "m1"
        assert lines[-1] == "data: [DONE]"

    async def test_metrics_endpoint_token_gate(self, client, monkeypatch):
        """/metrics 端点 token 门：未配置 token → fail-closed 401；配置后带 token 200。"""
        from llmpod.config import settings as settings_mod

        c, *_ = client
        # 默认（测试环境无 token）→ 401
        resp = await c.get("/metrics")
        assert resp.status_code == 401
        # 配 token 后：无 token 401 / 错 token 401 / 对 token 200 文本
        monkeypatch.setattr(settings_mod.settings, "metrics_token", "devtok", raising=False)
        resp = await c.get("/metrics")
        assert resp.status_code == 401
        resp = await c.get("/metrics", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401
        resp = await c.get("/metrics", headers={"Authorization": "Bearer devtok"})
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        # 打过几发请求后至少有 requests_total 指标族（前序用例已产生请求）
        assert "llmpod_proxy_requests" in resp.text or "llmpod_proxy_" in resp.text

    async def test_no_available_endpoint_503(self, client, monkeypatch, tmp_path):
        c, db, redis, app = client
        # 删掉唯一端点 → published 别名无 targets
        from llmpod.models import ModelEndpoint

        ep = next(iter(db.all(ModelEndpoint)))
        await db.delete(ep)
        # 重建快照（app 内 runtime holder）
        await app.state.proxy.rebuild(db)
        _attach_logger(app, db, tmp_path)

        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []}, headers=AUTH)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "no_available_endpoint"
        # 未触达上游的 503 不落账（维持原口径：request_log 只记真实转发）
        await asyncio.sleep(0.05)
        assert db.all(RequestLog) == []


class TestModelsList:
    async def test_lists_published_aliases_openai_format(self, client):
        c, *_ = client
        resp = await c.get("/v1/models", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        ids = [m["id"] for m in data["data"]]
        assert "m1" in ids
        assert "m2" not in ids  # unpublished 不列出

    async def test_rejected_models_list_carries_request_id(self, client):
        """鉴权拒绝也带号：/v1/models 不走推理管道的盖章出口，此处单独盖——
        SDK 启动轮询被 401 时贴号可反查访问日志。"""
        c, *_ = client
        resp = await c.get("/v1/models", headers={"Authorization": "Bearer sk-nope"})
        assert resp.status_code == 401
        assert resp.headers["x-request-id"]


def _add_endpoint_with_binding(db, *, route_id: int, name: str, endpoint_id: int, binding_id: int) -> None:
    """给 route_id 挂上一个健康端点成员（端点登记 + 发布中心绑成员两行）。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    db.preload(
        ModelEndpoint(
            id=endpoint_id,
            deployment_id=f"dep-{endpoint_id}",
            instance_id=None,
            engine_type="vllm",
            protocol="openai",
            api_base=f"http://engine-{name}.test",
            path="/v1/chat/completions",
            api_key=None,
            served_model=f"internal-{name}",
            name=name,
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


def _add_published_route(db, *, route_id: int, name: str, endpoint_id: int, binding_id: int) -> None:
    """模拟发布中心落库：新增一条 published 路由，并挂上健康端点成员。"""
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    db.preload(
        Route(
            id=route_id,
            name=name,
            status="published",
            input_price_per_mtok=None,
            output_price_per_mtok=None,
            failover_enabled=False,
            failover_max_retries=0,
            created_at=now,
            updated_at=now,
        )
    )
    _add_endpoint_with_binding(db, route_id=route_id, name=name, endpoint_id=endpoint_id, binding_id=binding_id)


def _open_model_whitelist(db) -> None:
    """放开 Key 的 allowed_models（空=全部可用）；须在首个请求前改，鉴权缓存随后才落。"""
    db.all(ApiKey)[0].allowed_models = []


class TestPublishTakesEffectWithoutRebuild:
    """发布后立即可用：快照未轮到的名字回源点查（新增路由 / 路由刚绑上成员）。

    轮询周期最长 30s，发布完立刻测试若走不到回源就会 404/503，与本组用例锁定的
    「发布即生效」相反。
    """

    async def test_new_route_usable_before_next_poll(self, client, monkeypatch):
        c, db, *_ = client
        _open_model_whitelist(db)
        # 先用一次请求把快照建起来（此时库里还没有 m3）
        assert (
            await c.post("/v1/chat/completions", json={"model": "ghost", "messages": []}, headers=AUTH)
        ).status_code == 404

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream_ok()), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _add_published_route(db, route_id=3, name="m3", endpoint_id=30, binding_id=30)

        resp = await c.post("/v1/chat/completions", json={"model": "m3", "messages": []}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["model"] == "m3"

    async def test_route_gaining_first_member_usable_before_next_poll(self, client, monkeypatch):
        """已存在但无候选的路由：绑上成员后立即可用（快照里 entry 非空、targets 为空）。"""
        c, db, redis, app = client
        ep = next(iter(db.all(ModelEndpoint)))
        await db.delete(ep)  # 唯一端点没了 → m1 无候选，但路由条目仍在
        await app.state.proxy.rebuild(db)
        # 快照里是「m1 存在但无候选」——只按名字判命中会漏掉这类，直接 503 等 30s
        assert app.state.proxy.snapshot.get("m1").targets == ()

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream_ok()), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        m1 = next(r for r in db.all(Route) if r.name == "m1")
        _add_endpoint_with_binding(db, route_id=m1.id, name="m1", endpoint_id=31, binding_id=31)

        resp = await c.post("/v1/chat/completions", json={"model": "m1", "messages": []}, headers=AUTH)
        assert resp.status_code == 200

    async def test_new_route_resolved_once_per_ttl(self, client, monkeypatch):
        """回源命中的结论同样短期缓存：发布后到下一轮轮询之间灌流量不逐请求回源。"""
        c, db, *_ = client
        _open_model_whitelist(db)
        await c.get("/v1/models", headers=AUTH)  # 建快照

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream_ok()), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _add_published_route(db, route_id=3, name="m3", endpoint_id=30, binding_id=30)

        statements = []
        real_execute = db.execute

        async def _counting_execute(stmt):
            statements.append(stmt)
            return await real_execute(stmt)

        monkeypatch.setattr(db, "execute", _counting_execute)
        for _ in range(3):
            resp = await c.post("/v1/chat/completions", json={"model": "m3", "messages": []}, headers=AUTH)
            assert resp.status_code == 200

        assert len(statements) == 3  # 首次回源三查（路由/成员/端点），其后两次命中缓存

    async def test_unknown_model_miss_is_cached(self, client, monkeypatch):
        """未命中回源结果短期缓存：同一名字重复探测不逐请求打库。

        （名字互不相同的枚举挡不住——缓存只吸收重复查询。这是与 TTL 无关的固有
        边界，真正的兜底是鉴权先于回源、且回源只做点查。）
        """
        c, db, *_ = client
        await c.get("/v1/models", headers=AUTH)  # 建快照（不计入下方计数）

        statements = []
        real_execute = db.execute

        async def _counting_execute(stmt):
            statements.append(stmt)
            return await real_execute(stmt)

        monkeypatch.setattr(db, "execute", _counting_execute)
        for _ in range(3):
            resp = await c.post("/v1/chat/completions", json={"model": "ghost", "messages": []}, headers=AUTH)
            assert resp.status_code == 404

        assert len(statements) == 1  # 首次回源，其后两次命中负缓存

    async def test_models_list_follows_snapshot_rebuild(self, client):
        """/v1/models 读快照：新发布名字随下一轮重建出现（≤ 一个轮询周期），
        不再逐请求查 PG——列表端点被 SDK 高频轮询，快照口径够用且零 DB 成本。"""
        c, db, app = client[0], client[1], client[3]
        _open_model_whitelist(db)
        await c.get("/v1/models", headers=AUTH)  # 建快照

        _add_published_route(db, route_id=3, name="m3", endpoint_id=30, binding_id=30)

        ids = [m["id"] for m in (await c.get("/v1/models", headers=AUTH)).json()["data"]]
        assert "m3" not in ids  # 快照未重建：列表暂缺（调用路径不受影响，走回源）

        await app.state.proxy.rebuild(db)
        ids = [m["id"] for m in (await c.get("/v1/models", headers=AUTH)).json()["data"]]
        assert "m3" in ids


class TestRequestLogging:
    async def test_nonstream_request_logged_with_usage_and_refs(self, client, monkeypatch, tmp_path):
        c, db, redis, app = client

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream_ok()), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _attach_logger(app, db, tmp_path)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.05)  # fire-and-forget 落账

        rows = db.all(RequestLog)
        assert len(rows) == 1
        row = rows[0]
        assert row.model_alias == "m1" and row.total_tokens == 5
        assert row.request_ref and row.response_ref  # 块引用
        assert row.conv_prefix_hash is not None  # 会话指纹
        assert row.status == "success"
        # 上游流向 + 重试（本地部署首试成功）
        assert row.upstream_type == "local"
        assert row.endpoint_id == 10
        assert row.retry_count == 0

    async def test_stream_request_logged_with_usage_and_latency(self, client, monkeypatch, tmp_path):
        c, db, redis, app = client

        def handler(request: httpx.Request) -> httpx.Response:
            chunks = [
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-m1",
                    "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
                },
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-m1",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ]
            body = b"".join(b"data: " + json.dumps(x).encode() + b"\n\n" for x in chunks) + b"data: [DONE]\n\n"
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _attach_logger(app, db, tmp_path)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [],
                "stream": True,
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.05)  # fire-and-forget 落账

        rows = db.all(RequestLog)
        assert len(rows) == 1
        row = rows[0]
        assert row.model_alias == "m1" and row.total_tokens == 2
        assert row.status == "success"
        assert row.request_ref and row.response_ref
        assert row.latency_ms is not None  # 流式落账也补端到端耗时

    async def test_failover_retry_logged_with_retry_count(self, client, monkeypatch, tmp_path):
        """首端点失败重试到次端点 → retry_count=1、endpoint_id 指向最终命中。"""
        c, db, redis, app = client
        _add_failover_endpoint(db)

        fake_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_fake_upstream_ok(fail_hosts=("engine-fail.test",))), timeout=10
        )
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _attach_logger(app, db, tmp_path)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.05)

        rows = db.all(RequestLog)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "success"
        assert row.retry_count == 1
        assert row.endpoint_id == 10  # 重试后命中低优先级原端点

    async def test_external_endpoint_logged_as_external(self, client, monkeypatch, tmp_path):
        """外部登记端点（deployment_id=NULL）→ upstream_type=external，上游名取端点 name。"""
        c, db, redis, app = client

        ep = db.all(ModelEndpoint)[0]
        ep.deployment_id = None
        ep.engine_type = "external"
        # 上游展示名取自端点 name，不再是可选的 provider_name（后者为空会让上游认不出来）。
        # 这里刻意让两者取不同值，锁住「落账的是端点名」这个口径。
        ep.name = "external-100"
        ep.provider_name = "OpenAI"

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream_ok()), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _attach_logger(app, db, tmp_path)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.05)

        row = db.all(RequestLog)[0]
        assert row.upstream_type == "external"
        assert row.provider_name == "external-100"  # 端点 name，非 provider_name="OpenAI"
        assert row.endpoint_id == 10
        assert row.engine == "external"

    async def test_upstream_error_logged_with_retry(self, client, monkeypatch, tmp_path):
        """上游全失败（failover 耗尽）→ 502 且 error 计量行落账（含重试次数与流向）。"""
        c, db, redis, app = client
        _add_failover_endpoint(db)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _attach_logger(app, db, tmp_path)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 502
        await asyncio.sleep(0.05)

        rows = db.all(RequestLog)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "error"
        assert row.http_status == 502
        assert row.retry_count == 1
        assert row.upstream_type == "local"
        assert row.error_message == "upstream HTTP 500"

    async def test_upstream_timeout_returns_504_not_hang(self, client, monkeypatch, tmp_path):
        """上游死亡/黑洞（读超时）→ 快速失败 504 + error 落账，绝不无限挂起。"""
        c, db, redis, app = client

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("engine dead: no bytes ever")

        fake_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(connect=1.0, read=0.2, write=1.0, pool=1.0),
        )
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _attach_logger(app, db, tmp_path)

        resp = await asyncio.wait_for(
            c.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=AUTH,
            ),
            timeout=5,
        )
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "upstream_timeout"
        await asyncio.sleep(0.05)

        rows = db.all(RequestLog)
        assert len(rows) == 1
        assert rows[0].status == "error"
        assert rows[0].http_status == 504

    async def test_streaming_first_byte_deadline(self, client, monkeypatch):
        """引擎「活着不回包」→ 流式请求按首字节墙钟失败，不等 read 空闲上限。

        用上游 sleep 模拟引擎假死；把 ttfb 压到 0.2s，断言快速拿到 504 而非挂起。
        """
        c, db, redis, app = client
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "upstream_ttfb_timeout", 0.2)

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(30)  # 假死：连上但永不回包
            return httpx.Response(200, json={})

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(60.0))
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        t0 = asyncio.get_event_loop().time()
        resp = await asyncio.wait_for(
            c.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=AUTH,
            ),
            timeout=5,
        )
        elapsed = asyncio.get_event_loop().time() - t0
        assert resp.status_code == 504, resp.text[:200]
        assert resp.json()["error"]["code"] == "upstream_timeout"
        assert elapsed < 3, f"首字节上限未生效（耗时 {elapsed:.2f}s）"

    async def test_nonstream_wall_clock_deadline(self, client, monkeypatch):
        """非流式：整体墙钟上限生效（首字节与整体不可区分，故整体限时）。"""
        c, db, redis, app = client
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "upstream_nonstream_timeout", 0.2)

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(30)
            return httpx.Response(200, json={})

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(60.0))
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        resp = await asyncio.wait_for(
            c.post(
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=AUTH,
            ),
            timeout=5,
        )
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "upstream_timeout"

    async def test_single_candidate_transport_failure_opens_breaker(self, client, monkeypatch):
        """唯一候选的连接级故障也要熔断——否则每个请求都挂满超时、流量持续灌黑洞。

        threshold=2：头两次连接失败各返回 502/504，第三次起直接 503（不再触达上游）。
        """
        c, db, redis, app = client
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "upstream_failure_threshold", 2)
        monkeypatch.setattr(settings, "upstream_cooldown_seconds", 60)
        # 用控制器当前 settings 重建 selector（app fixture 已在建 app 时读过 settings）
        app.state.proxy.selector = type(app.state.proxy.selector)(cooldown_seconds=60, failure_threshold=2)

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            raise httpx.ConnectError("engine gone")

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(1.0))
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        body = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}
        first = await c.post("/v1/chat/completions", json=body, headers=AUTH)
        second = await c.post("/v1/chat/completions", json=body, headers=AUTH)
        assert first.status_code == 502 and second.status_code == 502

        upstream_calls_before = len(calls)
        third = await c.post("/v1/chat/completions", json=body, headers=AUTH)
        assert third.status_code == 503, third.text[:200]
        assert third.json()["error"]["code"] == "no_available_endpoint"
        assert len(calls) == upstream_calls_before, "熔断后不得再触达上游"

    async def test_single_candidate_wall_clock_timeout_no_breaker(self, client, monkeypatch):
        """墙钟预算耗尽（我们自己的超时口径）≠ 端点死了：唯一候选不熔断——
        一次长 prefill 吃满 60s 预算，不该把之后本该秒回的短请求也连坐成 60s 503。"""
        c, db, redis, app = client
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "upstream_failure_threshold", 2)
        monkeypatch.setattr(settings, "upstream_cooldown_seconds", 60)
        monkeypatch.setattr(settings, "upstream_nonstream_timeout", 0.2)
        app.state.proxy.selector = type(app.state.proxy.selector)(cooldown_seconds=60, failure_threshold=2)

        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            await asyncio.sleep(30)  # 假死：触发平台侧墙钟超时（非 socket 错误）
            return httpx.Response(200, json={})

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(60.0))
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        body = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}
        for _ in range(3):  # 连续 3 次墙钟超时（超过 threshold=2）
            resp = await asyncio.wait_for(c.post("/v1/chat/completions", json=body, headers=AUTH), timeout=5)
            assert resp.status_code == 504

        fourth = await asyncio.wait_for(c.post("/v1/chat/completions", json=body, headers=AUTH), timeout=5)
        assert fourth.status_code == 504, "端点不该被熔断（仍应触达上游拿墙钟 504）"
        assert len(calls) == 4, "每次请求都应触达上游（无 503 no_available_endpoint）"


class TestEmbeddingsAndRerankRoutes:
    async def test_embeddings_route(self, client, monkeypatch):
        c, *_ = client

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": "internal-m1",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                    "usage": {"prompt_tokens": 2, "total_tokens": 2},
                },
            )

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        resp = await c.post("/v1/embeddings", json={"model": "m1", "input": ["x"]}, headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["model"] == "m1"

    async def test_embeddings_billed_and_logged(self, client, monkeypatch, tmp_path):
        """embeddings 有真实 usage：计费之外必须落 request_log——租户账单与
        rebuild_quota_counters 都从调用日志聚合，只扣钱不落账无法对账。"""
        c, db, redis, app = client
        _attach_logger(app, db, tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": "internal-m1",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                    "usage": {"prompt_tokens": 2, "total_tokens": 2},
                },
            )

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)

        resp = await c.post("/v1/embeddings", json={"model": "m1", "input": ["x"]}, headers=AUTH)
        assert resp.status_code == 200
        await asyncio.sleep(0.05)
        rows = db.all(RequestLog)
        assert len(rows) == 1
        assert rows[0].operation == "embeddings"
        assert rows[0].total_tokens == 2

    async def test_rerank_route(self, client, monkeypatch):
        c, *_ = client

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "r",
                    "model": "internal-m1",
                    "results": [{"index": 0, "relevance_score": 0.5}],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )

        fake_http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_rerank_http_client", lambda *a, **k: fake_http)

        resp = await c.post(
            "/v1/rerank",
            json={
                "model": "m1",
                "query": "q",
                "documents": ["a"],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "m1"

    async def test_rerank_request_logged_with_usage(self, client, monkeypatch, tmp_path):
        """rerank 也走统一计费流水：全操作 request_log 落账 + usage 入账。"""
        c, db, redis, app = client

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "r",
                    "model": "internal-m1",
                    "results": [{"index": 0, "relevance_score": 0.5}],
                    "usage": {"prompt_tokens": 4, "total_tokens": 4},
                },
            )

        fake_http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_rerank_http_client", lambda *a, **k: fake_http)
        _attach_logger(app, db, tmp_path)

        resp = await c.post(
            "/v1/rerank",
            json={
                "model": "m1",
                "query": "q",
                "documents": ["a"],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.05)

        rows = db.all(RequestLog)
        assert len(rows) == 1
        row = rows[0]
        assert row.operation == "rerank" and row.model_alias == "m1"
        assert row.total_tokens == 4 and row.status == "success"
        assert row.request_ref and row.response_ref


class TestCachedTokenPricing:
    """缓存命中 token 独立计价：cost = 命中×缓存价 + (prompt-命中)×输入价 + 输出×输出价。"""

    async def _logged_row(self, client, monkeypatch, tmp_path, *, usage, prices):
        c, db, redis, app = client
        # 注入定价（快照经 rebuild 读配置态）
        from llmpod.models import Route

        route = next(r for r in db.all(Route) if r.name == "m1")
        route.input_price_per_mtok, route.output_price_per_mtok, route.cached_input_price_per_mtok = prices
        await app.state.proxy.rebuild(db)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "internal-m1",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": usage,
                },
            )

        fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
        monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)
        _attach_logger(app, db, tmp_path)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        await asyncio.sleep(0.05)
        rows = db.all(RequestLog)
        assert len(rows) == 1
        return rows[0]

    async def test_cost_splits_cached_and_uncached(self, client, monkeypatch, tmp_path):
        """prompt=10 命中 6：cost = 6×0.1 + (10-6)×1.0 + 2×2.0（每百万单位）。"""
        from decimal import Decimal

        row = await self._logged_row(
            client,
            monkeypatch,
            tmp_path,
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 6},
            },
            prices=(Decimal("1"), Decimal("2"), Decimal("0.1")),
        )
        assert row.cached_tokens == 6
        assert row.prompt_tokens == 10
        # 6*0.1 + 4*1.0 + 2*2.0 = 8.6（每百万 token 单位下的 8.6e-6 元）
        assert (
            row.cost
            == (Decimal(6) * Decimal("0.1") + Decimal(4) * Decimal("1") + Decimal(2) * Decimal("2")) / 1_000_000
        )

    async def test_cost_exact_value(self, client, monkeypatch, tmp_path):
        """精确费用断言（单位：元；usage 为 token 数）。"""
        from decimal import Decimal

        row = await self._logged_row(
            client,
            monkeypatch,
            tmp_path,
            usage={
                "prompt_tokens": 1_000_000,
                "completion_tokens": 0,
                "total_tokens": 1_000_000,
                "prompt_tokens_details": {"cached_tokens": 1_000_000},
            },
            prices=(Decimal("1"), Decimal("2"), Decimal("0.1")),
        )
        # 全部命中：1M×0.1/M = 0.1
        assert row.cached_tokens == 1_000_000
        assert row.cost == Decimal("0.1")

    async def test_cost_falls_back_to_full_price_when_cached_price_unset(self, client, monkeypatch, tmp_path):
        """未配置缓存价（None）→ 命中部分按全价计（兼容旧行为）。"""
        from decimal import Decimal

        row = await self._logged_row(
            client,
            monkeypatch,
            tmp_path,
            usage={
                "prompt_tokens": 1_000_000,
                "completion_tokens": 0,
                "total_tokens": 1_000_000,
                "prompt_tokens_details": {"cached_tokens": 1_000_000},
            },
            prices=(Decimal("1"), Decimal("2"), None),
        )
        assert row.cached_tokens == 1_000_000
        assert row.cost == Decimal("1")  # 全价

    async def test_cached_not_reported_uses_full_prompt_price(self, client, monkeypatch, tmp_path):
        """引擎未上报缓存（cached_tokens=None）→ 全量 prompt 按输入价（现行为不变）。"""
        from decimal import Decimal

        row = await self._logged_row(
            client,
            monkeypatch,
            tmp_path,
            usage={"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000},
            prices=(Decimal("1"), Decimal("2"), Decimal("0.1")),
        )
        assert row.cached_tokens is None
        assert row.cost == Decimal("1")
