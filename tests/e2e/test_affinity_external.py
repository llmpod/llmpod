"""会话亲和真实上游校验：多轮对话粘在同一个上游，独立会话仍能分散。

与本地部署栈的区别：一条路由在两个协议下各挂**两个外部端点**（同一个真实上游、
同一个模型），选路真的有得选——单端点栈看不出「粘住」与「随机」的差别。
观测口径是管理面调用日志的 ``endpoint_id`` 与 ``route_reason``（亲和 / 加权随机），
不是上游响应内容：同一上游的两个端点返回的业务数据相同。

除 `LLMPOD_E2E=1` 外还需配置外部上游（同时提供 OpenAI 与 Anthropic 两个协议面）：

    LLMPOD_E2E=1 \\
    LLMPOD_E2E_BASE_URL=http://127.0.0.1:8080 \\
    LLMPOD_E2E_PROXY_BASE_URL=http://127.0.0.1:8090 \\
    LLMPOD_E2E_AFFINITY_BASE=http://<host>:<port> \\
    LLMPOD_E2E_AFFINITY_KEY=sk-... \\
    LLMPOD_E2E_AFFINITY_OPENAI_MODEL=<openai 协议模型名> \\
    LLMPOD_E2E_AFFINITY_ANTHROPIC_MODEL=<anthropic 协议模型名> \\
    uv run pytest tests/e2e/test_affinity_external.py -v

未配置外部上游时本文件 skip（`make test-e2e` 的隔离栈没有数据面，同样跳过）。
"""

from __future__ import annotations

import os
import time

import httpx
import pytest
from _client import ok

BASE = os.environ.get("LLMPOD_E2E_AFFINITY_BASE", "").rstrip("/")
KEY = os.environ.get("LLMPOD_E2E_AFFINITY_KEY", "")
OPENAI_MODEL = os.environ.get("LLMPOD_E2E_AFFINITY_OPENAI_MODEL", "")
ANTHROPIC_MODEL = os.environ.get("LLMPOD_E2E_AFFINITY_ANTHROPIC_MODEL", "")

pytestmark = pytest.mark.skipif(
    not (BASE and KEY and OPENAI_MODEL and ANTHROPIC_MODEL),
    reason="未配置外部上游（LLMPOD_E2E_AFFINITY_BASE/KEY/OPENAI_MODEL/ANTHROPIC_MODEL）",
)

PROTOCOLS = ("openai", "anthropic")


@pytest.fixture(scope="session")
def px() -> httpx.Client:
    """数据面客户端（默认本机 8090；独立栈请用 LLMPOD_E2E_PROXY_BASE_URL 改指）。"""
    base = os.environ.get("LLMPOD_E2E_PROXY_BASE_URL", "http://127.0.0.1:8090")
    with httpx.Client(base_url=base, timeout=300.0) as c:
        yield c


def _conversation(session: str, turn: int) -> list[dict]:
    """第 turn 轮对话：前几轮问答固定，末尾是本轮新问题（消息只往后追加）。"""
    messages: list[dict] = []
    for i in range(1, turn):
        messages.append({"role": "user", "content": f"{session} 第 {i} 问"})
        messages.append({"role": "assistant", "content": f"第 {i} 答"})
    messages.append({"role": "user", "content": f"{session} 第 {turn} 问"})
    return messages


def _send(px, route_name: str, plaintext: str, protocol: str, messages: list[dict], **extra):
    """按协议发送一轮对话；extra 是附加在请求体上的字段。"""
    if protocol == "openai":
        return px.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {plaintext}"},
            json={"model": route_name, "max_tokens": 8, "messages": messages, **extra},
        )
    return px.post(
        "/v1/messages",
        headers={"x-api-key": plaintext},
        json={"model": route_name, "max_tokens": 8, "messages": messages, **extra},
    )


def _request_id(resp, protocol: str) -> str:
    return resp.headers["request-id"] if protocol == "anthropic" else resp.headers["x-request-id"]


def _client_hint(protocol: str, hint: str) -> dict:
    """客户端自报的会话标识（不该影响选路）：各协议惯用字段不同。"""
    if protocol == "anthropic":
        return {"metadata": {"user_id": hint}}
    return {"metadata": {"user_id": hint}, "prompt_cache_key": hint}


def _wait_logs(admin_client, model: str, request_ids: list[str], timeout: float = 120) -> dict[str, dict]:
    """等这些请求的调用日志全部落账，按 request_id 索引返回。"""
    pending = set(request_ids)
    rows: dict[str, dict] = {}
    deadline = time.time() + timeout
    while pending and time.time() < deadline:
        data = ok(admin_client, "GET", "/api/v1/admin/request-logs", params={"model": model, "page_size": 100})
        for item in data["items"]:
            request_id = item.get("request_id")
            if request_id in pending:
                rows[request_id] = item
                pending.discard(request_id)
        if pending:
            time.sleep(2)
    assert not pending, f"调用日志未落账：{sorted(pending)}"
    return rows


@pytest.fixture(scope="module")
def route(admin_client, uniq):
    """一条路由：两个协议各两个端点（同上游同模型，仅端点行不同）。"""
    name = uniq("affinity")
    endpoints: dict[str, list[int]] = {}
    for protocol, served_model in (("openai", OPENAI_MODEL), ("anthropic", ANTHROPIC_MODEL)):
        ids: list[int] = []
        for tag in ("a", "b"):
            endpoint = ok(
                admin_client,
                "POST",
                "/api/v1/admin/endpoints",
                json={
                    "protocol": protocol,
                    "api_base": BASE,
                    "served_model": served_model,
                    "api_key": KEY,
                    "name": uniq(f"ep-{protocol}-{tag}"),
                    "skip_probe": True,  # 真连由用例请求本身完成：不通会直接红
                },
            )
            ids.append(endpoint["id"])
        endpoints[protocol] = ids

    bindings = [
        {"endpoint_id": endpoint_id, "weight": 1, "priority": 0}
        for protocol_ids in endpoints.values()
        for endpoint_id in protocol_ids
    ]
    created = ok(
        admin_client,
        "POST",
        "/api/v1/admin/publishes",
        json={
            "name": name,
            "model_type": "llm",
            "failover_enabled": True,
            "failover_max_retries": 1,
            "bindings": bindings,
        },
    )
    route_id = created["route"]["id"]
    yield {"id": route_id, "name": name, "endpoints": endpoints}

    # 自带清场：单独跑本文件也不留残余（路由先删，端点随后）
    admin_client.post(f"/api/v1/admin/publishes/{route_id}/unpublish")
    admin_client.post(f"/api/v1/admin/publishes/{route_id}/delete")
    for protocol_ids in endpoints.values():
        for endpoint_id in protocol_ids:
            admin_client.post(f"/api/v1/admin/endpoints/{endpoint_id}/delete")


@pytest.fixture(scope="module")
def key(admin_client, route):
    """平台 Key（只放行本路由），等数据面快照同步到新路由。"""
    created = ok(
        admin_client,
        "POST",
        "/api/v1/admin/api-keys",
        json={"name": f"{route['name']}-key", "allowed_models": [route["name"]]},
    )
    plaintext = created.get("plaintext") or created.get("key") or (created.get("api_key") or {}).get("plaintext")
    assert plaintext, f"签发 Key 的响应里没有明文：{str(created)[:300]}"
    key_id = (created.get("api_key") or {}).get("id") or created.get("id") or created.get("key_id")

    _wait_proxy_route(plaintext, route["name"])
    yield plaintext

    if key_id is not None:
        admin_client.post(f"/api/v1/admin/api-keys/{key_id}/delete")


def _wait_proxy_route(plaintext: str, route_name: str, timeout: float = 180):
    """数据面配置快照按轮询周期重建：等 /v1/models 出现新路由。"""
    base = os.environ.get("LLMPOD_E2E_PROXY_BASE_URL", "http://127.0.0.1:8090")
    deadline = time.time() + timeout
    with httpx.Client(base_url=base, timeout=30.0) as c:
        while time.time() < deadline:
            resp = c.get("/v1/models", headers={"Authorization": f"Bearer {plaintext}"})
            if resp.status_code == 200 and route_name in [m.get("id") for m in resp.json().get("data") or []]:
                return
            time.sleep(3)
    raise TimeoutError(f"数据面未同步到路由 {route_name}")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_multi_turn_conversation_sticks_to_one_endpoint(admin_client, px, route, key, protocol):
    """同一会话连续 6 轮全部落在同一个端点，且第 2 轮起由亲和接管。"""
    request_ids: list[str] = []
    sent_messages: list[list[dict]] = []
    for turn in range(1, 7):
        messages = _conversation(f"粘性会话-{protocol}", turn)
        resp = _send(px, route["name"], key, protocol, messages)
        assert resp.status_code == 200, f"第 {turn} 轮失败：{resp.text[:500]}"
        request_ids.append(_request_id(resp, protocol))
        sent_messages.append(messages)

    rows = _wait_logs(admin_client, route["name"], request_ids)
    endpoints = [rows[request_id].get("endpoint_id") for request_id in request_ids]
    reasons = [rows[request_id].get("route_reason") for request_id in request_ids]

    assert len(set(endpoints)) == 1, f"会话被拆到多个上游：{endpoints}"
    assert endpoints[0] in route["endpoints"][protocol]
    assert reasons[0] == "weighted", f"冷会话首轮应走加权随机：{reasons}"
    assert reasons[1:] == ["affinity"] * (len(reasons) - 1), f"命中亲和后应持续粘住：{reasons}"

    # 亲和只决定去向，不改请求内容：命中轮次的请求体逐字透传（仅 model 换上游模型名）
    detail = ok(admin_client, "GET", f"/api/v1/admin/request-logs/{request_ids[1]}")
    forwarded = detail.get("request_body")
    assert forwarded is not None, "调用详情里没有请求体快照"
    assert forwarded["messages"] == sent_messages[1]
    assert forwarded["model"] == route["name"]
    assert set(forwarded) == {"model", "max_tokens", "messages"}, "请求体被注入了额外参数"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_distinct_sessions_are_not_forced_together(admin_client, px, route, key, protocol):
    """没有任何共享前缀的独立会话仍按权重分散到两个端点（不是退化成单端点）。"""
    request_ids: list[str] = []
    for i in range(1, 13):
        messages = [{"role": "user", "content": f"独立会话 {protocol} #{i}"}]
        resp = _send(px, route["name"], key, protocol, messages)
        assert resp.status_code == 200, f"第 {i} 个会话失败：{resp.text[:500]}"
        request_ids.append(_request_id(resp, protocol))

    rows = _wait_logs(admin_client, route["name"], request_ids)
    endpoints = {rows[request_id].get("endpoint_id") for request_id in request_ids}

    assert endpoints <= set(route["endpoints"][protocol])
    assert len(endpoints) == 2, f"12 个独立会话全落在同一上游，加权随机未生效：{endpoints}"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_client_session_hints_do_not_reroute(admin_client, px, route, key, protocol):
    """换客户端自报的会话标识（内容不变）仍粘在同一端点：路由键是内容不是标识。"""
    first = _send(px, route["name"], key, protocol, _conversation(f"提示词无关-{protocol}", 1))
    assert first.status_code == 200, first.text[:500]
    first_id = _request_id(first, protocol)
    baseline = _wait_logs(admin_client, route["name"], [first_id])[first_id].get("endpoint_id")

    follow_ids: list[str] = []
    for hint in ("device-alpha", "device-beta"):
        resp = _send(
            px,
            route["name"],
            key,
            protocol,
            _conversation(f"提示词无关-{protocol}", 2),
            **_client_hint(protocol, hint),
        )
        assert resp.status_code == 200, f"提示词 {hint} 的轮次失败：{resp.text[:500]}"
        follow_ids.append(_request_id(resp, protocol))

    rows = _wait_logs(admin_client, route["name"], follow_ids)
    endpoints = [rows[request_id].get("endpoint_id") for request_id in follow_ids]
    reasons = [rows[request_id].get("route_reason") for request_id in follow_ids]

    assert endpoints == [baseline, baseline], f"客户端标识改变了选路：{endpoints} vs {baseline}"
    assert reasons == ["affinity", "affinity"], reasons
