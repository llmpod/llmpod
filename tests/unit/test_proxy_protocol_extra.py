"""协议层 embeddings / rerank 转发与整形。

- embeddings：httpx 直通（不实例化官方 SDK），model 回写别名 + 透传；
- rerank：httpx 直通（无官方 SDK），model 回写别名 + 结果字段透传。
"""

from __future__ import annotations

import json

import httpx
import pytest

from llmpod.apps.proxyserver.protocol import OpenAIEmbeddingsAdapter, RerankAdapter
from llmpod.apps.proxyserver.snapshot import RouteTarget


def _target(path: str = "/v1/embeddings", api_key: str | None = None) -> RouteTarget:
    return RouteTarget(
        endpoint_id=1,
        deployment_id="dep-1",
        api_base="http://engine.test",
        path=path,
        served_model="internal-embed",
        engine_type="vllm",
        weight=1,
        priority=0,
        api_key=api_key,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


class TestEmbeddings:
    async def test_model_rewrite_and_passthrough(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": "internal-embed",
                    "data": [
                        {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
                    ],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                    "engine_private": 1,
                },
            )

        result = await OpenAIEmbeddingsAdapter().forward(
            _client(handler),
            _target(),
            alias_name="embed-m1",
            payload={"model": "embed-m1", "input": ["hello"]},
        )

        assert seen["body"]["model"] == "internal-embed"
        assert result.body["model"] == "embed-m1"
        assert result.body["engine_private"] == 1  # 未知字段透传
        assert result.body["data"][0]["embedding"] == [0.1, 0.2]
        assert result.body["usage"]["total_tokens"] == 3

    async def test_error_typed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "busy"}})

        with pytest.raises(httpx.HTTPStatusError):
            await OpenAIEmbeddingsAdapter().forward(
                _client(handler),
                _target(),
                alias_name="e",
                payload={"model": "e", "input": ["x"]},
            )


class TestRerank:
    async def test_passthrough_with_model_rewrite(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "rerank-1",
                    "model": "internal-embed",
                    "results": [{"index": 0, "relevance_score": 0.9}],
                    "usage": {"prompt_tokens": 5, "total_tokens": 5},
                },
            )

        result = await RerankAdapter().forward(
            httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10),
            _target(path="/v1/rerank"),
            alias_name="embed-m1",
            payload={"model": "embed-m1", "query": "q", "documents": ["a", "b"]},
        )

        assert seen["url"].endswith("/v1/rerank")
        assert seen["body"]["model"] == "internal-embed"
        assert result.body["model"] == "embed-m1"
        assert result.body["results"][0]["relevance_score"] == 0.9

    async def test_api_key_sent_as_bearer(self):
        """外部 rerank 供应商走 Bearer 鉴权——api_key 必须上 header。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"id": "r", "model": "internal-embed", "results": []})

        await RerankAdapter().forward(
            httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10),
            _target(api_key="sk-ext"),
            alias_name="embed-m1",
            payload={"model": "embed-m1", "query": "q", "documents": []},
        )
        assert seen["auth"] == "Bearer sk-ext"

    async def test_base_url_normalized_without_path(self):
        """path 缺省时 api_base 裸 host 补 /v1；已带 /v1 不重复拼。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"id": "r", "model": "m", "results": []})

        adapter = RerankAdapter()
        await adapter.forward(
            httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10),
            _target(path=None),
            alias_name="e",
            payload={"model": "e", "query": "q", "documents": []},
        )
        assert seen["url"].endswith("/v1/rerank")

    async def test_request_error_in_upstream_errors(self):
        """连接失败（RequestError）属 upstream_errors——调用方 failover 可捕获。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(httpx.RequestError):
            await RerankAdapter().forward(
                httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10),
                _target(),
                alias_name="e",
                payload={"model": "e", "query": "q", "documents": []},
            )
        assert httpx.RequestError in RerankAdapter.upstream_errors

    async def test_upstream_500_raises_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        with pytest.raises(httpx.HTTPStatusError):
            await RerankAdapter().forward(
                httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10),
                _target(),
                alias_name="e",
                payload={"model": "e", "query": "q", "documents": []},
            )
