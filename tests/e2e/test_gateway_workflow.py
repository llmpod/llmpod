"""真实 HTTP 网关闭环：管理面、数据面、PG/Redis 均真实；仅推理上游受控。

先单独运行 _gateway_upstream.py（也可供 test_affinity_external.py 使用），再配置：
  LLMPOD_E2E=1
  LLMPOD_E2E_BASE_URL=http://127.0.0.1:18084
  LLMPOD_E2E_PROXY_BASE_URL=http://127.0.0.1:18094
  LLMPOD_E2E_GATEWAY_UPSTREAM_BASE=http://10.0.2.2:18091/v1  # 从应用容器可达的地址
  LLMPOD_E2E_GATEWAY_UPSTREAM_KEY=e2e-upstream              # 与服务一致，可省略
管理员凭据沿用公共 admin_client fixture 的环境配置。

每次创建独立资源并通过 API 清理；调用日志/钱包流水作为历史审计记录保留。
这验证 ARM 应用的协议、权限、计费链路，不代表真实模型推理或 GPU 验证。
"""

from __future__ import annotations

from contextlib import ExitStack
from decimal import Decimal
import json
import os
import time

import httpx
import pytest
from _client import expect_error, ok, page
from _gateway_upstream import (
    ANTHROPIC_MODEL,
    CHAT_MODEL,
    COMPLETION_TOKENS,
    DEFAULT_KEY,
    EMBEDDING_MODEL,
    PROMPT_TOKENS,
    REPLY,
    RERANK_MODEL,
)

UPSTREAM_BASE = os.environ.get("LLMPOD_E2E_GATEWAY_UPSTREAM_BASE", "").rstrip("/")
UPSTREAM_KEY = os.environ.get("LLMPOD_E2E_GATEWAY_UPSTREAM_KEY", DEFAULT_KEY)
pytestmark = pytest.mark.skipif(not UPSTREAM_BASE, reason="需设置 LLMPOD_E2E_GATEWAY_UPSTREAM_BASE（受控上游）")


def _wait_models(px, key, *, present=(), absent=()):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = px.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 200, response.text
        models = {item["id"] for item in response.json()["data"]}
        if set(present) <= models and not set(absent) & models:
            return
        time.sleep(2)
    pytest.fail(f"路由快照未同步：需出现 {present}，需消失 {absent}，当前 {models}")


def _wait_logs(admin_client, tenant_id, key_id, expected):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        data = page(
            admin_client,
            "GET",
            "/api/v1/admin/request-logs",
            params={"tenant_id": tenant_id, "api_key_id": key_id, "page_size": 100},
        )
        rows = {row["request_id"]: row for row in data["items"]}
        if expected.keys() <= rows.keys():
            return rows
        time.sleep(2)
    pytest.fail(f"调用日志未落账：{expected.keys() - rows.keys()}")


def _tenant(admin_client, base_url, uniq, cleanup, blocked=()):
    username, password = uniq("gw-admin"), "GatewayTest0rd!"
    tenant = ok(
        admin_client,
        "POST",
        "/api/v1/admin/tenants",
        json={
            "name": uniq("gw-tenant"),
            "admin_username": username,
            "admin_password": password,
            "blocked_route_ids": list(blocked),
        },
    )
    cleanup.callback(ok, admin_client, "POST", f"/api/v1/admin/tenants/{tenant['id']}/delete")
    client = cleanup.enter_context(httpx.Client(base_url=base_url, timeout=30))
    login = ok(client, "POST", "/api/v1/auth/login", json={"username": username, "password": password})
    assert login["access_domain"] == "tenant" and not login["is_superuser"]
    client.headers["Authorization"] = f"Bearer {login['token']}"
    return tenant, client


def _key(client, admin_client, cleanup, uniq, models):
    created = ok(
        client,
        "POST",
        "/api/v1/tenant/api-keys",
        json={"name": uniq("gw-key"), "allowed_models": models, "budget_limit": "1"},
    )
    cleanup.callback(ok, admin_client, "POST", f"/api/v1/admin/api-keys/{created['api_key']['id']}/delete")
    assert created["plaintext"].startswith("sk-")
    return created


def _stream(px, path, headers, payload, protocol):
    with px.stream("POST", path, headers=headers, json=payload) as response:
        assert response.status_code == 200, response.read().decode()
        assert response.headers["content-type"].startswith("text/event-stream")
        request_id = response.headers["request-id" if protocol == "anthropic" else "x-request-id"]
        lines = []
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            lines.append(data)
            # SDK 在终止帧立即关闭 HTTP 流，仍应完成计费、解冻及日志落库。
            if data == "[DONE]" or (protocol == "anthropic" and json.loads(data).get("type") == "message_stop"):
                break
    frames = [json.loads(line) for line in lines if line != "[DONE]"]
    if protocol == "openai":
        assert lines[-1] == "[DONE]"
        assert all(frame["model"] == payload["model"] for frame in frames)
        assert (
            "".join(
                choice.get("delta", {}).get("content", "") for frame in frames for choice in frame.get("choices", [])
            )
            == REPLY
        )
        assert frames[-1]["usage"]["total_tokens"] == PROMPT_TOKENS + COMPLETION_TOKENS
    else:
        assert frames[0]["type"] == "message_start"
        assert frames[0]["message"]["model"] == payload["model"]
        assert frames[-1]["type"] == "message_stop"
        assert "".join(frame["delta"]["text"] for frame in frames if frame["type"] == "content_block_delta") == REPLY
    return request_id


def test_gateway_protocols_billing_isolation_and_revocation(admin_client, base_url, uniq):
    endpoint_ids, route_ids, tenant_ids = [], [], []
    with ExitStack() as cleanup:
        px = cleanup.enter_context(
            httpx.Client(
                base_url=os.environ.get("LLMPOD_E2E_PROXY_BASE_URL", "http://127.0.0.1:8090"),
                timeout=60,
            )
        )
        assert px.get("/v1/models").status_code == 401
        endpoints = {}
        for kind, protocol, served_model in (
            ("chat", "openai", CHAT_MODEL),
            ("anthropic", "anthropic", ANTHROPIC_MODEL),
            ("embedding", "openai", EMBEDDING_MODEL),
            ("rerank", "openai", RERANK_MODEL),
        ):
            endpoint = ok(
                admin_client,
                "POST",
                "/api/v1/admin/endpoints",
                json={
                    "name": uniq(f"gw-{kind}"),
                    "protocol": protocol,
                    "api_base": UPSTREAM_BASE,
                    "api_key": UPSTREAM_KEY,
                    "served_model": served_model,
                    # 登记探测固定发 chat；embedding/rerank 的真实连接由后面的对应请求验证。
                    "skip_probe": kind in ("embedding", "rerank"),
                },
            )
            endpoint_ids.append(endpoint["id"])
            endpoints[kind] = endpoint
            cleanup.callback(ok, admin_client, "POST", f"/api/v1/admin/endpoints/{endpoint['id']}/delete")
            assert "api_key" not in endpoint
        for kind in ("chat", "anthropic"):
            probe = ok(
                admin_client, "POST", "/api/v1/admin/endpoints/test", json={"endpoint_id": endpoints[kind]["id"]}
            )
            assert probe["healthy"] and probe["status_code"] == 200, probe

        routes = {}
        for kind, members in (("llm", ("chat", "anthropic")), ("embedding", ("embedding",)), ("rerank", ("rerank",))):
            published = ok(
                admin_client,
                "POST",
                "/api/v1/admin/publishes",
                json={
                    "name": uniq(f"gw-{kind}"),
                    "model_type": kind,
                    "input_price_per_mtok": "2",
                    "output_price_per_mtok": "4",
                    "failover_enabled": False,
                    "bindings": [
                        {"endpoint_id": endpoints[member]["id"], "weight": 1, "priority": 0} for member in members
                    ],
                },
            )
            route = published["route"]
            assert route["status"] == "published"
            routes[kind] = route
            route_ids.append(route["id"])
            cleanup.callback(ok, admin_client, "POST", f"/api/v1/admin/publishes/{route['id']}/delete")

        tenant_a, client_a = _tenant(admin_client, base_url, uniq, cleanup)
        tenant_b, client_b = _tenant(admin_client, base_url, uniq, cleanup, blocked=[routes["llm"]["id"]])
        tenant_ids.extend([tenant_a["id"], tenant_b["id"]])
        ok(
            admin_client,
            "POST",
            f"/api/v1/admin/tenants/{tenant_a['id']}/wallet/adjust",
            json={"amount": "1", "mode": "add", "reason": "controlled gateway E2E"},
        )
        names = [route["name"] for route in routes.values()]
        key_a = _key(client_a, admin_client, cleanup, uniq, names)
        key_b = _key(client_b, admin_client, cleanup, uniq, [])
        key_id = key_a["api_key"]["id"]
        headers = {"Authorization": f"Bearer {key_a['plaintext']}"}
        _wait_models(px, key_a["plaintext"], present=names)
        _wait_models(px, key_b["plaintext"], absent=[routes["llm"]["name"]])

        counted = px.post(
            "/v1/messages/count_tokens",
            headers={"x-api-key": key_a["plaintext"]},
            json={"model": routes["llm"]["name"], "messages": [{"role": "user", "content": "count"}]},
        )
        assert counted.status_code == 200 and counted.json()["input_tokens"] == PROMPT_TOKENS, counted.text

        # 每个成功请求都关联同一 request_id 的落账与钱包流水，避免只看总数。
        expected = {}
        for protocol in ("openai", "anthropic"):
            path = "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
            auth = (
                {"x-api-key": key_a["plaintext"], "anthropic-version": "2023-06-01"}
                if protocol == "anthropic"
                else headers
            )
            for streaming in (False, True):
                payload = {
                    "model": routes["llm"]["name"],
                    "max_tokens": 8,
                    "stream": streaming,
                    "messages": [{"role": "user", "content": uniq("gateway-prompt")}],
                }
                if streaming:
                    request_id = _stream(px, path, auth, payload, protocol)
                else:
                    response = px.post(path, headers=auth, json=payload)
                    assert response.status_code == 200, response.text
                    body = response.json()
                    assert body["model"] == payload["model"]
                    text = (
                        body["content"][0]["text"]
                        if protocol == "anthropic"
                        else body["choices"][0]["message"]["content"]
                    )
                    assert text == REPLY
                    request_id = response.headers["request-id" if protocol == "anthropic" else "x-request-id"]
                endpoint = endpoints["anthropic" if protocol == "anthropic" else "chat"]
                expected[request_id] = (
                    payload,
                    endpoint,
                    "anthropic.messages" if protocol == "anthropic" else "chat.completions",
                    COMPLETION_TOKENS,
                )

        for kind, operation, request in (
            ("embedding", "embeddings", {"input": ["alpha", "beta"]}),
            ("rerank", "rerank", {"query": "alpha", "documents": ["alpha", "beta"]}),
        ):
            payload = {"model": routes[kind]["name"], **request}
            response = px.post(f"/v1/{operation}", headers=headers, json=payload)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["model"] == payload["model"]
            if kind == "embedding":
                assert [row["embedding"] for row in body["data"]] == [[0.25, 0.5, 0.75]] * 2
            else:
                assert [row["index"] for row in body["results"]] == [0, 1]
                assert body["results"][0]["relevance_score"] > body["results"][1]["relevance_score"]
            expected[response.headers["x-request-id"]] = (payload, endpoints[kind], operation, 0)

        rows = _wait_logs(admin_client, tenant_a["id"], key_id, expected)
        assert rows.keys() == expected.keys(), "独立 Key 出现非本测试请求或重复调用"
        total_cost = Decimal("0")
        for request_id, (payload, endpoint, operation, completion) in expected.items():
            row = rows[request_id]
            cost = Decimal(PROMPT_TOKENS * 2 + completion * 4) / 1_000_000
            total_cost += cost
            assert row["status"] == "success" and row["http_status"] == 200, row
            assert row["tenant_id"] == tenant_a["id"] and row["api_key_id"] == key_id
            assert row["operation"] == operation and row["model_alias"] == payload["model"]
            assert row["endpoint_id"] == endpoint["id"] and row["served_model"] == endpoint["served_model"]
            assert row["upstream_type"] == "external" and row["usage_estimated"] is False
            assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (
                PROMPT_TOKENS,
                completion,
                PROMPT_TOKENS + completion,
            )
            assert Decimal(row["cost"]) == cost
            detail = ok(client_a, "GET", f"/api/v1/tenant/request-logs/{request_id}")
            assert detail["request_body"] == payload and detail["response_body"] is not None
            assert not {"endpoint_id", "served_model", "provider_name", "error_detail"} & detail.keys()

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            transactions = page(client_a, "GET", "/api/v1/tenant/wallet/transactions", params={"page_size": 100})
            consumes = [row for row in transactions["items"] if row["type"] == "consume"]
            wallet = ok(client_a, "GET", "/api/v1/tenant/wallet")
            if len(consumes) == len(expected) and Decimal(wallet["frozen_balance"]) == 0:
                break
            time.sleep(2)
        assert len(consumes) == len(expected), transactions
        assert {row["request_id"] for row in consumes} == expected.keys()
        for transaction in consumes:
            assert transaction["api_key_id"] == key_id
            assert Decimal(transaction["amount"]) == -Decimal(rows[transaction["request_id"]]["cost"])
        assert Decimal(wallet["balance"]) == Decimal("1") - total_cost
        assert Decimal(wallet["available"]) == Decimal(wallet["balance"])
        keys = ok(client_a, "GET", "/api/v1/tenant/api-keys")
        assert Decimal(next(key for key in keys if key["id"] == key_id)["budget_spent"]) == total_cost

        # B 默认可用其他模型，但被封禁 LLM；Key、日志和钱包同时保持租户隔离。
        assert routes["llm"]["name"] not in {row["name"] for row in ok(client_b, "GET", "/api/v1/tenant/models")}
        assert key_id not in {key["id"] for key in ok(client_b, "GET", "/api/v1/tenant/api-keys")}
        expect_error(client_b, "POST", f"/api/v1/tenant/api-keys/{key_id}/disable", status=404)
        expect_error(client_b, "GET", f"/api/v1/tenant/request-logs/{next(iter(expected))}", status=404)
        assert page(client_b, "GET", "/api/v1/tenant/request-logs", params={"api_key_id": key_id})["total"] == 0
        wallet_b = ok(client_b, "GET", "/api/v1/tenant/wallet", params={"tenant_id": tenant_a["id"]})
        assert wallet_b["tenant_id"] == tenant_b["id"] and Decimal(wallet_b["balance"]) == 0
        expect_error(client_b, "GET", "/api/v1/admin/endpoints", status=403)
        expect_error(
            client_b,
            "POST",
            "/api/v1/tenant/api-keys",
            status=422,
            json={"name": uniq("gw-blocked"), "allowed_models": [routes["llm"]["name"]]},
        )
        chat = {"model": routes["llm"]["name"], "messages": [{"role": "user", "content": "denied"}], "max_tokens": 8}
        denied = px.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key_b['plaintext']}"}, json=chat)
        assert denied.status_code == 403, denied.text
        restricted = _key(client_a, admin_client, cleanup, uniq, [routes["embedding"]["name"]])
        denied = px.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {restricted['plaintext']}"}, json=chat
        )
        assert denied.status_code == 403, denied.text

        ok(client_a, "POST", f"/api/v1/tenant/api-keys/{key_id}/disable")
        assert px.get("/v1/models", headers=headers).status_code == 401
        assert px.post("/v1/chat/completions", headers=headers, json=chat).status_code == 401
        ok(client_a, "POST", f"/api/v1/tenant/api-keys/{key_id}/enable")
        for route in routes.values():
            assert (
                ok(admin_client, "POST", f"/api/v1/admin/publishes/{route['id']}/unpublish")["status"] == "unpublished"
            )
        _wait_models(px, key_a["plaintext"], absent=names)
        denied = px.post("/v1/chat/completions", headers=headers, json=chat)
        assert denied.status_code == 403, denied.text
        assert Decimal(ok(client_a, "GET", "/api/v1/tenant/wallet")["balance"]) == Decimal("1") - total_cost

    assert not set(endpoint_ids) & {row["id"] for row in ok(admin_client, "GET", "/api/v1/admin/endpoints")}
    assert not set(route_ids) & {row["route"]["id"] for row in ok(admin_client, "GET", "/api/v1/admin/publishes")}
    for tenant_id in tenant_ids:
        expect_error(admin_client, "GET", f"/api/v1/admin/tenants/{tenant_id}", status=404)
        assert ok(admin_client, "GET", "/api/v1/admin/api-keys", params={"tenant_id": tenant_id}) == []
