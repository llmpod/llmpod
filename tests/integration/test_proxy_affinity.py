"""会话亲和端到端：同一会话的多轮请求落回同一上游，两种换端策略行为分明。

场景对应「一个模型挂多个供应商」：两个 anthropic 端点同属一条路由，逐请求加权随机
会把同一会话打散、把上游前缀缓存全部冲掉。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from _fakes import FakeRedis, FakeSession

import llmpod.apps.proxyserver.routes.inference as app_mod
from llmpod.apps.proxyserver.app import create_proxy_app
from llmpod.config.enums import AffinityFailoverPolicy
from llmpod.config.settings import settings
from llmpod.models import ApiKey, ModelEndpoint, RequestLog, Route, RouteBinding

CLIENT_KEY = "sk-affinity"
HOST_A = "ep-a.test"
HOST_B = "ep-b.test"

_SSE = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"model":"upstream","usage":{"input_tokens":3,"output_tokens":1}}}\n\n'
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
    "event: message_delta\n"
    'data: {"type":"message_delta","usage":{"output_tokens":2},"delta":{"stop_reason":"end_turn"}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _turn(n: int) -> dict:
    """第 n 轮请求（单一会话，消息只往后追加）。"""
    messages: list[dict] = []
    for i in range(1, n):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    messages.append({"role": "user", "content": f"q{n}"})
    return {"model": "m1", "max_tokens": 32, "messages": messages}


def _seeded_db() -> FakeSession:
    """一条路由 + 两个同协议端点（加权随机下逐请求随机选择端点）。"""
    db = FakeSession()
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    db.preload(
        Route(
            id=1,
            name="m1",
            status="published",
            origin="manual",
            model_type="llm",
            description=None,
            context_length=None,
            tags=[],
            input_price_per_mtok=None,
            output_price_per_mtok=None,
            cached_input_price_per_mtok=None,
            failover_enabled=True,
            failover_max_retries=2,
            created_at=now,
            updated_at=now,
        )
    )
    for endpoint_id, host in ((11, HOST_A), (12, HOST_B)):
        db.preload(
            ModelEndpoint(
                id=endpoint_id,
                deployment_id=None,
                model_deployment_replica_id=None,
                instance_id=None,
                engine_type="external",
                protocol="anthropic",
                api_base=f"https://{host}",
                path=None,
                api_key=None,
                served_model=f"upstream-{endpoint_id}",
                name=host,
                provider_name=f"provider-{endpoint_id}",
                context_window=None,
                operations=[],
                capabilities=[],
                enabled=True,
                health_status="healthy",
                last_synced_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        db.preload(
            RouteBinding(
                id=endpoint_id,
                route_id=1,
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


class _Upstream:
    """假上游：记录每次实际打到的 host 与请求，可动态指定哪些 host 返回 500。"""

    def __init__(self):
        self.hosts: list[str] = []
        self.requests: list[httpx.Request] = []
        self.failing: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        self.requests.append(request)
        if request.url.host in self.failing:
            return httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, text=_SSE, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "upstream",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), timeout=10)

    def clear(self):
        self.hosts.clear()
        self.requests.clear()


@asynccontextmanager
async def _fresh_env(monkeypatch):
    """按当前 settings 装配一套完整环境（装配期开关须先 monkeypatch 再建 app）。"""
    upstream = _Upstream()
    monkeypatch.setattr(app_mod, "get_anthropic_client", lambda *a, **k: upstream.client())

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
        yield c, upstream, app, db

    app.dependency_overrides.clear()


@pytest.fixture
async def env(monkeypatch):
    async with _fresh_env(monkeypatch) as ctx:
        yield ctx


def _seed_selector_rng(app):
    """给已装配 app 的选择器注入播种随机源，消掉加权随机的概率性。

    app.py 构造 EndpointSelector 时不注入 rng（生产走系统熵，正确）；集成断言
    「两种端点都出现过」依赖 8 次采样的随机结果（两等权全落一端约 0.8%/用例），
    播种后确定性成立，不再赌 CI 运气。
    """
    app.state.proxy.selector._rng = random.Random(7)


async def _post(client, turn: int) -> httpx.Response:
    return await client.post("/v1/messages", json=_turn(turn), headers={"x-api-key": CLIENT_KEY})


class TestConversationStickiness:
    async def test_second_turn_lands_on_same_upstream(self, env):
        c, upstream, _app, _db = env

        assert (await _post(c, 1)).status_code == 200
        first_host = upstream.hosts[-1]

        assert (await _post(c, 2)).status_code == 200

        assert upstream.hosts[-1] == first_host

    async def test_whole_conversation_stays_on_one_upstream(self, env):
        """多轮全程只打到一个上游——这正是前缀缓存能命中的前提。"""
        c, upstream, _app, _db = env

        for turn in range(1, 9):
            assert (await _post(c, turn)).status_code == 200

        assert len(set(upstream.hosts)) == 1

    async def test_distinct_conversations_are_not_forced_together(self, env, monkeypatch):
        """亲和按内容派生：不同会话互不影响，仍能分散到不同上游（不是退化成单端点）。"""
        c, upstream, app, _db = env
        _seed_selector_rng(app)

        for i in range(1, 9):
            payload = {"model": "m1", "max_tokens": 32, "messages": [{"role": "user", "content": f"独立会话-{i}"}]}
            assert (await c.post("/v1/messages", json=payload, headers={"x-api-key": CLIENT_KEY})).status_code == 200

        assert set(upstream.hosts) == {HOST_A, HOST_B}

    async def test_shared_preamble_does_not_suck_new_sessions_together(self, env, monkeypatch):
        """共用同一段系统提示词的全新会话不该被吸到同一端点。

        「只有垫场」不是可命中边界：否则 8 个内容互不相同的新会话会全落同一上游
        （共享提示词 = 共享最短边界），绕过负载均衡。
        """
        c, upstream, app, _db = env
        _seed_selector_rng(app)

        for i in range(1, 9):
            payload = {
                "model": "m1",
                "max_tokens": 32,
                "system": "同一段共享系统提示词",
                "messages": [{"role": "user", "content": f"各不相同的问题 {i}"}],
            }
            assert (await c.post("/v1/messages", json=payload, headers={"x-api-key": CLIENT_KEY})).status_code == 200

        assert set(upstream.hosts) == {HOST_A, HOST_B}


class TestRoutingKeyAndPayload:
    """亲和只决定去向：不读客户端自报的会话标识，也不改请求内容。

    客户端标识（请求头 / ``metadata.user_id`` / ``prompt_cache_key``）不可信、也没有
    会话粒度；用它选路等于把路由权交给客户端。请求内容同样不做任何拼装——数据面
    是裸透传，命中亲和也不能借机补字段或覆盖参数。
    """

    async def test_changing_client_hints_keeps_the_conversation_together(self, env):
        c, upstream, _app, _db = env
        assert (await _post(c, 1)).status_code == 200
        first_host = upstream.hosts[-1]

        payload = {
            **_turn(2),
            "metadata": {"user_id": "另一台设备"},
            "prompt_cache_key": "另一个会话标识",
        }
        headers = {
            "x-api-key": CLIENT_KEY,
            "x-affinity-key": "another-session-id",
            "conversation-id": "another-session-id",
        }
        assert (await c.post("/v1/messages", json=payload, headers=headers)).status_code == 200

        assert upstream.hosts[-1] == first_host

    async def test_same_client_hint_does_not_merge_distinct_conversations(self, env):
        """同一客户端标识下的不同对话照常分散：粘性来自内容，不是标识。"""
        c, upstream, app, _db = env
        _seed_selector_rng(app)

        for i in range(1, 9):
            payload = {
                "model": "m1",
                "max_tokens": 32,
                "metadata": {"user_id": "同一台设备"},
                "messages": [{"role": "user", "content": f"互不相关的对话 {i}"}],
            }
            assert (await c.post("/v1/messages", json=payload, headers={"x-api-key": CLIENT_KEY})).status_code == 200

        assert set(upstream.hosts) == {HOST_A, HOST_B}

    async def test_affinity_hit_forwards_the_body_verbatim(self, env):
        """命中亲和后请求体逐字透传：只把 model 换成上游模型名，不注入任何参数。"""
        c, upstream, _app, _db = env
        assert (await _post(c, 1)).status_code == 200
        upstream.clear()

        payload = {
            **_turn(2),
            "temperature": 0.31,
            "top_p": 0.77,
            "metadata": {"trace": "t1"},
        }
        assert (await c.post("/v1/messages", json=payload, headers={"x-api-key": CLIENT_KEY})).status_code == 200

        forwarded = json.loads(upstream.requests[-1].content)
        assert forwarded["messages"] == payload["messages"]
        assert forwarded["temperature"] == 0.31
        assert forwarded["top_p"] == 0.77
        assert forwarded["metadata"] == {"trace": "t1"}
        assert forwarded["model"] in {"upstream-11", "upstream-12"}  # 仅换上游模型名
        assert set(forwarded) == set(payload)  # 没有新增字段


class TestFailoverPolicies:
    """亲和命中后端失败：两种策略都要能切换，且行为确实不同。"""

    async def _bind_first_turn(self, c, upstream) -> str:
        assert (await _post(c, 1)).status_code == 200
        return upstream.hosts[-1]

    async def test_rebind_fails_over_and_moves_binding(self, env, monkeypatch):
        monkeypatch.setattr(settings, "affinity_failover_policy", AffinityFailoverPolicy.REBIND.value)
        c, upstream, _app, _db = env
        bound_host = await self._bind_first_turn(c, upstream)

        upstream.failing.add(bound_host)
        upstream.clear()

        assert (await _post(c, 2)).status_code == 200  # 换端成功
        survivor = upstream.hosts[-1]
        assert survivor != bound_host

        # 亲和已改绑到真正服务成功的那端：下一轮不再回到故障端
        upstream.clear()
        assert (await _post(c, 3)).status_code == 200
        assert upstream.hosts == [survivor]

    async def test_strict_refuses_to_switch_upstream(self, env, monkeypatch):
        monkeypatch.setattr(settings, "affinity_failover_policy", AffinityFailoverPolicy.STRICT.value)
        c, upstream, _app, _db = env
        bound_host = await self._bind_first_turn(c, upstream)

        upstream.failing.add(bound_host)
        upstream.clear()

        assert (await _post(c, 2)).status_code == 502  # 缓存优先：直接失败，不甩到别的上游
        assert upstream.hosts == [bound_host]  # 另一端点一次都没被碰过

    async def test_strict_never_switches_even_after_cooldown(self, env, monkeypatch):
        """strict 的「不换端」在端点熔断后同样成立：冷却窗口内快速失败（503），
        不把会话甩给别的端点。"""
        monkeypatch.setattr(settings, "affinity_failover_policy", AffinityFailoverPolicy.STRICT.value)
        c, upstream, _app, _db = env
        bound_host = await self._bind_first_turn(c, upstream)

        upstream.failing.add(bound_host)
        upstream.clear()

        # 默认 failure_threshold=2：前两轮失败把端点送进冷却；第三轮起端点不可用
        assert (await _post(c, 2)).status_code == 502
        assert (await _post(c, 3)).status_code == 502
        assert (await _post(c, 4)).status_code == 503  # 熔断窗口：宁可失败不换端
        assert upstream.hosts == [bound_host, bound_host]  # 另一端点一次都没被碰过

    async def test_strict_still_fails_over_when_affinity_missed(self, env, monkeypatch):
        """未命中亲和的请求不受 strict 约束——它没有缓存可保，照常重试。"""
        monkeypatch.setattr(settings, "affinity_failover_policy", AffinityFailoverPolicy.STRICT.value)
        c, upstream, _app, _db = env

        upstream.failing.add(HOST_A)
        upstream.failing.add(HOST_B)
        upstream.clear()

        # 首轮无绑定 → 走普通 failover 预算：两个端点都被试过
        assert (await _post(c, 1)).status_code == 502
        assert set(upstream.hosts) == {HOST_A, HOST_B}

    async def test_unknown_policy_value_does_not_break_requests(self, env, monkeypatch):
        monkeypatch.setattr(settings, "affinity_failover_policy", "typo-value")
        c, upstream, _app, _db = env
        await self._bind_first_turn(c, upstream)
        assert (await _post(c, 2)).status_code == 200


class TestAffinityDisabled:
    async def test_disabled_affinity_restores_plain_random(self, monkeypatch):
        """开关关掉后回到纯加权随机：多轮不再保证同一上游。"""
        monkeypatch.setattr(settings, "affinity_enabled", False)

        async with _fresh_env(monkeypatch) as (c, upstream, app, _db):
            _seed_selector_rng(app)
            for turn in range(1, 9):
                assert (await _post(c, turn)).status_code == 200

            assert set(upstream.hosts) == {HOST_A, HOST_B}

    async def test_boundary_hashes_skipped_when_disabled(self, monkeypatch):
        """亲和关闭时不白算边界 hash（消费侧都不看，省一遍 O(消息数) 开销）。"""
        import llmpod.apps.proxyserver.inference_handler as handler_mod

        calls = {"n": 0}

        def spy(operation, payload):
            calls["n"] += 1
            return None

        monkeypatch.setattr(settings, "affinity_enabled", False)
        monkeypatch.setattr(handler_mod, "compute_boundary_hashes", spy)

        async with _fresh_env(monkeypatch) as (c, _upstream, _app, _db):
            assert (await _post(c, 1)).status_code == 200

        assert calls["n"] == 0


class TestStreamingHealthAccounting:
    """流式的成功判定在生成器收尾：断流端点要计失败、完整流才计成功并写亲和。"""

    @staticmethod
    def _endpoint_id_of(host: str) -> int:
        return 11 if host == HOST_A else 12

    async def test_midstream_death_counts_failure_and_opens_cooldown(self, env, monkeypatch):
        """上游吐完首帧即断流：端点计失败（不是成功）——连续两次后进入冷却。

        若此处被记成功，失败计数被反复清零、冷却永不触发，亲和还把半死端点固定给会话。
        """
        c, upstream, app, _db = env
        resp = await c.post("/v1/messages", json={**_turn(1), "stream": True}, headers={"x-api-key": CLIENT_KEY})
        assert resp.status_code == 200
        await resp.aread()
        dying_host = upstream.hosts[-1]

        async def dying_sse():  # 首帧吐完连接即断（模拟引擎 OOM / 重启）
            yield (
                b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
                b'{"input_tokens":3,"output_tokens":1}}}\n\n'
            )
            raise httpx.RemoteProtocolError("peer closed connection mid-stream")

        def cut_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=dying_sse(), headers={"content-type": "text/event-stream"})

        monkeypatch.setattr(app_mod, "get_anthropic_client", lambda *a, **k: _cut_client(cut_handler))

        selector = app.state.proxy.selector
        endpoint_id = self._endpoint_id_of(dying_host)
        for _ in range(2):  # 默认 failure_threshold=2：两次断流后进冷却
            with pytest.raises(httpx.HTTPError):
                resp = await c.post(
                    "/v1/messages",
                    json={**_turn(2), "stream": True},
                    headers={"x-api-key": CLIENT_KEY},
                )
                await resp.aread()

        state = selector._states.get(endpoint_id)
        assert state is not None and state.cooldown_until > 0  # 冷却被触发

        # 冷却窗口内该端点不再被选：亲和查表答不出（被熔断），下一轮落另一端点
        monkeypatch.undo()
        monkeypatch.setattr(app_mod, "get_anthropic_client", lambda *a, **k: upstream.client())
        upstream.clear()
        assert (await _post(c, 3)).status_code == 200
        assert upstream.hosts[-1] != dying_host

    async def test_completed_stream_binds_affinity(self, env):
        """完整消费的流式请求：收尾后写亲和——下一轮非流式请求落回同一上游。"""
        c, upstream, _app, _db = env

        resp = await c.post("/v1/messages", json={**_turn(1), "stream": True}, headers={"x-api-key": CLIENT_KEY})
        assert resp.status_code == 200
        await resp.aread()
        host_after_stream = upstream.hosts[-1]

        assert (await _post(c, 2)).status_code == 200
        assert upstream.hosts[-1] == host_after_stream


def _cut_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


class TestPriorityNotOverridden:
    """亲和只作用于最高可用优先级组：主池恢复后，故障期落到降级组的会话要能回去。"""

    async def test_session_returns_to_top_pool_after_recovery(self, env):
        c, upstream, app, db = env
        # 把 B 抬成高优先级主池、A 是低优先级降级
        for binding in db.all(RouteBinding):
            binding.priority = 9 if binding.endpoint_id == 12 else 0
        await app.state.proxy.rebuild(db)

        upstream.failing.add(HOST_B)  # 主池故障：会话被挤到降级端 A 并绑上
        assert (await _post(c, 1)).status_code == 200
        assert upstream.hosts[-1] == HOST_A

        # 主池恢复（尚未触发熔断，避免冷却窗口干扰）：下一轮回到主池——低组的
        # 亲和绑定不能凌驾优先级，否则会话永久偏在降级/外部供应商端
        upstream.failing.discard(HOST_B)
        upstream.clear()
        assert (await _post(c, 2)).status_code == 200
        assert upstream.hosts[-1] == HOST_B

        # 回主池后由亲和接管，固定在主池内
        upstream.clear()
        assert (await _post(c, 3)).status_code == 200
        assert upstream.hosts[-1] == HOST_B


def _attach_logger(app, db, tmp_path):
    """接线 fire-and-forget 记账到测试 db + tmp_path 块存储。"""
    from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore
    from llmpod.apps.proxyserver.request_logger import RequestLogger

    app.state.proxy.logger = RequestLogger(db_factory=None, store=LocalFileBodyStore(root=tmp_path))
    app.state.proxy.log_db = db


class TestRouteReasonRecorded:
    """落账要能回答「为什么是这个上游」：亲和 / 负载均衡 / 故障换端一眼分开。"""

    async def _reasons(self, db, request_ids: list[str]) -> list[str]:
        """按请求顺序取各请求落账行的 route_reason。

        行插入顺序不可依赖（记账备料在线程池执行，并发任务完成可乱序）；响应头
        回显的 request-id 是每条计量行的稳定键，按号查行即与插入顺序彻底解耦。
        """
        await asyncio.sleep(0.05)  # 等 fire-and-forget 记账任务跑完
        reason_by_id = {row.request_id: row.route_reason for row in db.all(RequestLog)}
        return [reason_by_id[rid] for rid in request_ids]

    @staticmethod
    def _rid(resp) -> str:
        # 本文件全部打 /v1/messages（anthropic 操作），编号走 request-id 头
        return resp.headers["request-id"]

    async def test_cold_turn_is_weighted_then_affinity(self, env, tmp_path):
        c, _upstream, app, db = env
        _attach_logger(app, db, tmp_path)

        r1 = await _post(c, 1)
        r2 = await _post(c, 2)
        r3 = await _post(c, 3)
        assert r1.status_code == r2.status_code == r3.status_code == 200

        assert await self._reasons(db, [self._rid(r1), self._rid(r2), self._rid(r3)]) == [
            "weighted",  # 冷会话：还没有可亲和的端点
            "affinity",  # 上一轮已把前缀绑上
            "affinity",
        ]

    async def test_failover_reason_recorded(self, env, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "affinity_failover_policy", AffinityFailoverPolicy.REBIND.value)
        c, upstream, app, db = env
        _attach_logger(app, db, tmp_path)

        r1 = await _post(c, 1)
        assert r1.status_code == 200
        upstream.failing.add(upstream.hosts[-1])
        r2 = await _post(c, 2)
        assert r2.status_code == 200  # 换端成功

        reasons = await self._reasons(db, [self._rid(r1), self._rid(r2)])
        assert reasons[0] == "weighted"
        assert reasons[1] == "failover_weighted"  # 上游故障后才按优先级 + 权重换的端

    async def test_failure_row_carries_reason_of_last_attempt(self, env, tmp_path):
        c, upstream, app, db = env
        _attach_logger(app, db, tmp_path)

        r1 = await _post(c, 1)
        assert r1.status_code == 200
        upstream.failing.update({HOST_A, HOST_B})
        r2 = await _post(c, 2)
        assert r2.status_code == 502  # failover 耗尽

        reasons = await self._reasons(db, [self._rid(r1), self._rid(r2)])
        assert reasons[1].startswith("failover_")  # 失败行同样记下选路出处

    async def test_streaming_row_carries_reason(self, env, tmp_path):
        """流式走另一条落账路径（生成器 finally），同样要带上选路原因。"""
        c, _upstream, app, db = env
        _attach_logger(app, db, tmp_path)

        payload = {**_turn(1), "stream": True}
        resp = await c.post("/v1/messages", json=payload, headers={"x-api-key": CLIENT_KEY})
        assert resp.status_code == 200

        assert await self._reasons(db, [resp.headers["request-id"]]) == ["weighted"]


class TestReasonVocabulary:
    def test_all_four_reasons_are_distinct(self):
        from llmpod.apps.proxyserver.routing import route_reason

        values = {
            route_reason(retried=False, via_affinity=False),
            route_reason(retried=False, via_affinity=True),
            route_reason(retried=True, via_affinity=False),
            route_reason(retried=True, via_affinity=True),
        }
        assert values == {"weighted", "affinity", "failover_weighted", "failover_affinity"}


class TestReasonVisibility:
    def test_reason_is_admin_only(self):
        """选路策略属运营侧口径，与 upstream_type/provider_name 同档：租户视图剥离。"""
        from llmpod.apps.webserver.schemas.out_models import RequestLogAdminOut, RequestLogOut

        assert "route_reason" in RequestLogAdminOut.model_fields
        assert "route_reason" not in RequestLogOut.model_fields
