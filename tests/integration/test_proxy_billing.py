"""dispatch 接入钱包扣费集成测试（租户 Key + 定价 → hold/settle；余额不足 → 402）。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from _fakes import FakeSession

import llmpod.apps.proxyserver.routes.inference as app_mod
from llmpod.apps.proxyserver.app import create_proxy_app
from llmpod.apps.proxyserver.billing import BillingService, InsufficientBalance, KeyBudgetExceeded
from llmpod.models import ApiKey, ModelEndpoint, Route, RouteBinding, Tenant


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _seeded_db() -> FakeSession:
    """租户 Key(tenant_id=5) + 定价路由 m1 + 授权 + 1 端点。"""
    db = FakeSession()
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    db.preload(
        Route(
            id=1,
            name="m1",
            status="published",
            input_price_per_mtok=Decimal("2"),
            output_price_per_mtok=Decimal("4"),
            cached_input_price_per_mtok=None,
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
    db.preload(Tenant(id=5, name="t5", status="active", created_at=now, updated_at=now))
    db.preload(
        ApiKey(
            id=1,
            key_hash=_hash("sk-tenant"),
            tenant_id=5,
            name="k",
            allowed_models=["m1"],
            rpm_limit=None,
            tpm_limit=None,
            total_token_quota=None,
            model_token_quotas={},
            budget_limit=None,
            budget_spent=Decimal("0"),
            status="active",
            expires_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    return db


class _NullDb:
    """billing 短事务 session 桩：SQL 原语都被 _WiredBilling 换掉，只剩 commit 要应。

    这里刻意不实现 execute——原语若漏桩会直接 AttributeError 炸出来，而不是静默走过。
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


class _WiredBilling(BillingService):
    """真实计费编排 + 桩掉的落库：分支逻辑全走生产代码，只把 SQL 原语换成记录。

    手写一个「像生产那样判断平台 Key / 零成本」的假服务是重复实现生产分支（生产改一处、
    这里得同改），故改为继承真实服务；本文件验的是生产编排本身而非它的复印件——平台 Key
    只拦 Key 预算不碰钱包、guard 的幂等与零成本短路，全部取自生产实现。

    记录形状：``holds=(key_id, tenant_id, est_cost, request_id)``、
    ``settles=(tenant_id, actual, est)``、``refunds=(tenant_id, est)``、
    ``platform_spends=(key_id, amount)``、``platform_freezes=(key_id, est)``、
    ``platform_unfreezes=(key_id, est)``。
    """

    def __init__(self, *, hold_raises=None, platform_freeze_raises=None):
        super().__init__(db_factory=_NullDb)
        self.hold_raises = hold_raises
        self.platform_freeze_raises = platform_freeze_raises
        self.holds = []
        self.settles = []
        self.refunds = []
        self.platform_spends = []
        self.platform_freezes = []
        self.platform_unfreezes = []
        self.closed = []

    async def _freeze(self, db, *, key_id, tenant_id, amount):
        if self.hold_raises:
            raise self.hold_raises

    async def _freeze_key_budget(self, db, *, key_id, amount):
        if self.platform_freeze_raises:
            raise self.platform_freeze_raises
        self.platform_freezes.append((key_id, amount))

    async def _release_key_budget(self, db, *, key_id, est):
        self.platform_unfreezes.append((key_id, est))

    async def _insert_hold(self, db, *, tenant_id, key_id, route_id, request_id, est_amount, expires_at):
        self.holds.append((key_id, tenant_id, est_amount, request_id))

    async def _settle_balances(self, db, *, tenant_id, key_id, actual, est):
        self.settles.append((tenant_id, actual, est))
        return Decimal("0")

    async def _release_balances(self, db, *, tenant_id, key_id, est):
        self.refunds.append((tenant_id, est))

    async def _close_hold(self, db, *, request_id, status, close_reason=None):
        self.closed.append((request_id, status))
        return True

    async def _insert_ledger(self, db, **kw):
        pass

    async def _settle_key_budget(self, db, *, key_id, amount, est):
        self.platform_spends.append((key_id, amount))
        return SimpleNamespace(rowcount=1)


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = str(value)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def incrby(self, key, amount):
        self.store[key] = str(int(self.store.get(key, "0")) + amount)
        return int(self.store[key])

    async def expire(self, key, seconds):
        return True


@pytest.fixture
async def client():
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


AUTH = {"Authorization": "Bearer sk-tenant"}


async def _forward_ok(monkeypatch, *, usage):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "internal-m1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": usage,
            },
        )

    fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)


_SSE_OK = (
    b'data: {"id":"c","object":"chat.completion.chunk","created":1,'
    b'"model":"internal-m1","choices":[{"index":0,"delta":{"content":"hi"},'
    b'"finish_reason":null}]}\n\n'
    b"data: [DONE]\n\n"
)


def _forward_sse(monkeypatch) -> None:
    """把上游换成一段固定 SSE 流，供流式用例复用。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SSE_OK, headers={"content-type": "text/event-stream"})

    fake_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)
    monkeypatch.setattr(app_mod, "get_openai_client", lambda *a, **k: fake_client)


class TestBillingWiring:
    async def test_success_holds_and_settles(self, client, monkeypatch):
        c, db, redis, app = client
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        await _forward_ok(monkeypatch, usage=usage)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200

        # hold：est_prompt=ceil(2/4)=1，est_completion=4096 → 1×2/M + 4096×4/M
        assert len(billing.holds) == 1
        _, tenant_id, est_cost, hold_request_id = billing.holds[0]
        assert tenant_id == 5
        assert est_cost == Decimal("1") / 1_000_000 * Decimal("2") + Decimal("4096") / 1_000_000 * Decimal("4")
        assert hold_request_id is not None

        # settle：actual = 3×2/M + 2×4/M（request_id 由 hold 那条记录覆盖）
        assert len(billing.settles) == 1
        _, actual_cost, settled_est = billing.settles[0]
        assert actual_cost == Decimal("3") / 1_000_000 * Decimal("2") + Decimal("2") / 1_000_000 * Decimal("4")
        assert settled_est == est_cost
        # _close_hold 拿到同一个 request_id：holds 按 request_id 关行，这里断了
        # （settle 未透传）balance_hold 行会永远停在 frozen，钱包与流水静默分叉
        assert billing.closed == [(hold_request_id, "settled")]

    async def test_insufficient_balance_returns_402(self, client, monkeypatch):
        c, db, redis, app = client
        key = next(iter(db.all(ApiKey)))
        key.tpm_limit = 100
        app.state.proxy.billing = _WiredBilling(hold_raises=InsufficientBalance("insufficient balance"))

        await _forward_ok(monkeypatch, usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 402
        err = resp.json()["error"]
        assert err["code"] == "insufficient_quota"
        assert sum(int(v) for k, v in redis.store.items() if k.startswith("llmpod:rl:1:") and k.endswith(":tpm")) == 0

    async def test_platform_key_records_spend_without_wallet(self, client, monkeypatch):
        """平台 Key（tenant_id=None）：不冻钱包、不写流水；预算无上限（NULL=不限）时
        预扣级零 DB（鉴权快照直接放行），只记 Key 侧消费。"""
        c, db, redis, app = client
        key = next(iter(db.all(ApiKey)))
        key.tenant_id = None
        key.budget_limit = None  # 不限
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        await _forward_ok(monkeypatch, usage=usage)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200  # 无上限 = 不限
        # 钱包侧零动作：无冻结、无钱包结算、无退款
        assert billing.holds == [] and billing.settles == [] and billing.refunds == []
        # Key 侧：无预算占位（快照无上限 → hold 不进 DB）+ 消费入账（actual = 3×2/M + 2×4/M）
        assert billing.platform_freezes == []
        assert billing.platform_spends == [
            (1, Decimal("3") / 1_000_000 * Decimal("2") + Decimal("2") / 1_000_000 * Decimal("4"))
        ]

    async def test_platform_key_limited_freezes_budget_placeholder(self, client, monkeypatch):
        """平台 Key 设了上限（快照可见）：hold 走 Key 预算占位（原语级），结算并入释放。"""
        c, db, redis, app = client
        key = next(iter(db.all(ApiKey)))
        key.tenant_id = None
        key.budget_limit = Decimal("100")
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        await _forward_ok(monkeypatch, usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert len(billing.platform_freezes) == 1 and billing.platform_freezes[0][0] == 1
        assert billing.holds == [] and billing.settles == [] and billing.refunds == []

    async def test_platform_key_budget_limit_blocks_at_402(self, client, monkeypatch):
        """平台 Key 设了 budget_limit 就真拦截：预算原语抛 KeyBudgetExceeded → 402，
        与租户 Key 同错误码（inference_handler 现成映射复用）。"""
        c, db, redis, app = client
        key = next(iter(db.all(ApiKey)))
        key.tenant_id = None
        key.budget_limit = Decimal("0.000001")  # 远低于本次预估（真拦截就会 402）
        billing = _WiredBilling(platform_freeze_raises=KeyBudgetExceeded("key budget exceeded"))
        app.state.proxy.billing = billing

        await _forward_ok(monkeypatch, usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 402
        assert resp.json()["error"]["code"] == "insufficient_quota"
        # 被拒在预扣级：无任何入账
        assert billing.platform_spends == [] and billing.settles == []

    async def test_quota_rejection_before_hold(self, client, monkeypatch):
        """配额超限在预扣之前拒绝——不碰钱（无 hold/refund）。"""
        c, db, redis, app = client
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        # Key 配额 10 token；请求预估 prompt=ceil(2/4)=1……直接把已用计数填满
        redis.store["llmpod:quota:key:1"] = "10"
        from llmpod.models import ApiKey as _AK

        key = next(iter(db.all(_AK)))
        key.total_token_quota = 10

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "insufficient_quota"
        assert billing.holds == [] and billing.refunds == []

    async def test_no_candidate_endpoint_refunds_hold(self, client, monkeypatch):
        """路由 published 但协议组无可用端点 → 503 + 退款预扣（资金不悬空）。"""
        c, db, redis, app = client
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        from llmpod.models import ModelEndpoint as _ME

        ep = next(iter(db.all(_ME)))
        ep.enabled = False  # 移出候选 → 无 openai 成员
        await app.state.proxy.rebuild(db)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 503
        assert len(billing.holds) == 1
        assert len(billing.refunds) == 1
        _, refunded_est = billing.refunds[0]
        assert refunded_est == billing.holds[0][2]  # 退全额预扣

    async def test_upstream_failover_exhausted_refunds_hold(self, client, monkeypatch):
        """failover 循环内上游全失败 → 502 + 退款预扣。"""
        c, db, redis, app = client
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

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
        assert resp.status_code == 502
        assert len(billing.holds) == 1 and len(billing.refunds) == 1

    async def test_invalid_json_returns_400(self, client, monkeypatch):
        """畸形 JSON body → 400（而非 500）。"""
        c, *_ = client
        resp = await c.post(
            "/v1/chat/completions",
            content=b"not-json{",
            headers={**AUTH, "content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_json"

    async def test_missing_usage_settles_zero_and_refunds_hold(self, client, monkeypatch):
        """引擎不返回 usage → actual=None 按 0 元结算（全额退预扣），不静默吞钱。"""
        c, db, redis, app = client
        billing = _WiredBilling()
        app.state.proxy.billing = billing

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
                },
            )  # 无 usage 字段

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
        assert len(billing.holds) == 1
        assert len(billing.settles) == 1
        _, actual_cost, _ = billing.settles[0]
        # 生产 settle 把 None 归一成 0：balance 不动、frozen 全释放（不静默吞预扣）
        assert actual_cost == Decimal("0")

    async def test_streaming_settles_once_no_refund(self, client, monkeypatch):
        """流式：hold 一次、settle 一次、零 refund（回归：曾外层 release + 生成器 settle 双重调账）。"""
        c, db, redis, app = client
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        _forward_sse(monkeypatch)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert len(billing.holds) == 1
        assert len(billing.settles) == 1  # 生成器 finally 结算一次
        assert len(billing.refunds) == 0  # 外层 finally 不再兜底退款（mark_streaming 后 no-op）

    async def test_streaming_in_flight_gauge_returns_to_baseline(self, client, monkeypatch):
        """回归：流式请求正常消费后 in-flight gauge 归零（曾 double-dec → 变负）。

        enter_request 只在入口 +1；出口 dec 必须恰好一次——生成器 finally 释放一次，
        guard 的 __call__ finally 见 stream_exited 标记后不再重复 dec。
        """
        c, db, redis, app = client
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        from llmpod.apps.proxyserver import metrics as proxy_metrics

        before = proxy_metrics.in_flight_requests._value.get()

        _forward_sse(monkeypatch)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200

        after = proxy_metrics.in_flight_requests._value.get()
        assert after == before  # 消费完成后归零；double-dec 会得到 before - 1

    async def test_platform_key_streaming_gauge_returns_to_baseline(self, client, monkeypatch):
        """回归：平台 Key 流式请求的 in-flight gauge 同样归零。

        平台 Key 现在恒出 guard（此前为 None），计费钩子（mark_streaming/close）对它
        生效；按租户 Key 的同口径锁住 gauge 归零与钱包侧零动作。流式**消费完成**路径
        的归零由生成器 finally 保证（租户用例已锁），此处叠加平台分支断言。
        """
        c, db, redis, app = client
        key = next(iter(db.all(ApiKey)))
        key.tenant_id = None
        key.budget_limit = None  # 无上限，走纯占位路径
        billing = _WiredBilling()
        app.state.proxy.billing = billing

        from llmpod.apps.proxyserver import metrics as proxy_metrics

        before = proxy_metrics.in_flight_requests._value.get()

        _forward_sse(monkeypatch)

        resp = await c.post(
            "/v1/chat/completions",
            json={
                "model": "m1",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        assert billing.holds == [] and billing.refunds == []  # 钱包侧零动作
        assert proxy_metrics.in_flight_requests._value.get() == before
