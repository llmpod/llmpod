"""协议层 chat.completions 转发与响应整形。

假引擎经 httpx.MockTransport 注入（与 SDK 契约测试同模式）。锁定：
- 请求侧：model 换 served_model、未知参数 extra_body 透传；
- 响应侧：model 回写别名（别名契约：客户端看到的 model 必须是其请求过的别名），其余
  全量透传（业界网关通行做法：白名单整形会静默吞掉上游新字段如
  reasoning_content，vLLM 扩展字段 token_ids/stop_reason 等原样转发）；
- 流式：逐 chunk model 回写、usage 在末 chunk；
- 错误：上游 HTTP/连接/解析异常向上抛（路由层归一化）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from llmpod.apps.proxyserver.protocol import OpenAIChatAdapter
from llmpod.apps.proxyserver.snapshot import RouteTarget


def _target(
    api_base="http://engine.test", served_model="internal-qwen3", api_key=None, path="/v1/chat/completions"
) -> RouteTarget:
    return RouteTarget(
        endpoint_id=1,
        deployment_id="dep-1",
        api_base=api_base,
        path=path,
        served_model=served_model,
        engine_type="vllm",
        weight=1,
        priority=0,
        protocol="openai",
        api_key=api_key,
    )


def _openai_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


def _sse_lines(*chunks: dict) -> bytes:
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
    return body


class TestNonStreamForward:
    async def test_model_rewrite_and_unknown_fields_passthrough(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 123,
                    "model": "internal-qwen3",
                    "vllm_private": {"latency": 0.5},  # 顶层未知字段 → 透传
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            )

        client = _openai_client(handler)
        result = await OpenAIChatAdapter().forward(
            client,
            _target(),
            alias_name="qwen3-8b",
            payload={"model": "qwen3-8b", "messages": [{"role": "user", "content": "hi"}]},
        )

        # 请求侧：model 换成引擎内部名
        assert seen["body"]["model"] == "internal-qwen3"
        assert seen["path"] == "/v1/chat/completions"
        # 响应侧：model 回写别名，其余透传
        assert result.body["model"] == "qwen3-8b"
        assert result.body["vllm_private"] == {"latency": 0.5}
        assert result.body["usage"]["total_tokens"] == 5
        assert result.body["choices"][0]["message"]["content"] == "hi"

    async def test_extra_body_passthrough(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        result = await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(),
            alias_name="a",
            payload={"model": "a", "messages": [], "future_engine_param": 42},
        )
        assert seen["body"]["future_engine_param"] == 42
        assert result.body["model"] == "a"

    async def test_non_object_response_passthrough_not_crash(self):
        """200 但顶层非对象（数组/标量）→ 原样透传、不崩、usage=None。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[1, 2, 3])

        result = await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(),
            alias_name="qwen3-8b",
            payload={"model": "qwen3-8b", "messages": []},
        )
        assert result.body == [1, 2, 3]  # 原样透传，不重写 model
        assert result.usage is None

    async def test_api_key_set_as_bearer_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(api_key="sk-ext"),
            alias_name="a",
            payload={"model": "a", "messages": []},
        )
        assert seen["auth"] == "Bearer sk-ext"

    async def test_no_auth_header_when_key_missing(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(),
            alias_name="a",
            payload={"model": "a", "messages": []},
        )
        assert seen["auth"] is None


class TestStreamForward:
    async def test_chunks_rewritten_streamed_and_final_usage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse_lines(
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                },
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "delta": {"content": "he"}, "finish_reason": None}],
                    "engine_extra": 1,
                },
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        chunks = []
        result = await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(),
            alias_name="qwen3-8b",
            payload={"model": "qwen3-8b", "messages": [], "stream": True},
        )
        async for ev in result.stream:
            chunks.append(ev.wire)

        payloads = [json.loads(c.removeprefix(b"data: ").rstrip()) for c in chunks[:-1]]
        assert chunks[-1] == b"data: [DONE]\n\n"
        assert all(p["model"] == "qwen3-8b" for p in payloads)  # 逐 chunk 回写
        assert payloads[1]["engine_extra"] == 1  # chunk 顶层未知字段透传
        assert payloads[-1]["usage"]["total_tokens"] == 5  # 末 chunk usage

    async def test_reasoning_content_delta_passthrough(self):
        """思考模型帧 delta.reasoning_content 必须透传（白名单时代静默丢失的回归锁）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse_lines(
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "reasoning_content": "思考中"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "delta": {"reasoning": "more thinking"}, "finish_reason": None}],
                },
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "delta": {"content": "答"}, "finish_reason": None}],
                },
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        result = await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(),
            alias_name="qwen3-8b",
            payload={"model": "qwen3-8b", "messages": [], "stream": True},
        )
        events = [ev async for ev in result.stream]
        chunks = [ev.wire for ev in events]
        assert events[0].billable_text == "思考中"
        assert events[1].billable_text == "more thinking"

        first = json.loads(chunks[0].removeprefix(b"data: ").rstrip())
        delta = first["choices"][0]["delta"]
        assert delta["role"] == "assistant"
        assert delta["reasoning_content"] == "思考中"
        third = json.loads(chunks[2].removeprefix(b"data: ").rstrip())
        assert third["choices"][0]["delta"]["content"] == "答"

    async def test_stream_non_object_chunk_passthrough(self):
        """流式 data 非对象（标量）→ 原样透传、不崩，后续 [DONE] 正常收尾。"""
        body = b"data: 42\n\n" + b"data: [DONE]\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        result = await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(),
            alias_name="a",
            payload={"model": "a", "messages": [], "stream": True},
        )
        chunks = [ev.wire async for ev in result.stream]
        assert chunks[0] == b"data: 42\n\n"  # 标量原样透传
        assert chunks[-1] == b"data: [DONE]\n\n"

    async def test_stream_options_include_usage_injected(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=_sse_lines(
                    {
                        "id": "c",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "internal-qwen3",
                        "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}],
                    },
                    {
                        "id": "c",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "internal-qwen3",
                        "choices": [],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                ),
                headers={"content-type": "text/event-stream"},
            )

        result = await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(),
            alias_name="a",
            payload={"model": "a", "messages": [], "stream": True},
        )
        async for _ in result.stream:
            pass

        assert seen["body"]["stream_options"] == {"include_usage": True}

    async def test_stream_connection_error_raises_in_forward(self):
        """流式上游 5xx 在 forward 内同步抛（eager 首事件），failover 循环可捕获。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"message": "overloaded"}})

        with pytest.raises(httpx.HTTPStatusError):
            await OpenAIChatAdapter().forward(
                _openai_client(handler),
                _target(),
                alias_name="a",
                payload={"model": "a", "messages": [], "stream": True},
            )


class TestErrorMapping:
    async def test_engine_429_raises_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "slow down"}})

        with pytest.raises(httpx.HTTPStatusError):
            await OpenAIChatAdapter().forward(
                _openai_client(handler),
                _target(),
                alias_name="a",
                payload={"model": "a", "messages": []},
            )


class TestUrlConstruction:
    """真机发现（2026-08-24）：ModelEndpoint.api_base 不带 /v1（编排侧写 host:port），
    openai 转发需把 base_url 归一化到版本根再拼 /chat/completions，否则引擎 404。"""

    async def _forward_url(self, api_base: str) -> str:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "internal-qwen3",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(api_base=api_base, path=None),
            alias_name="a",
            payload={"model": "a", "messages": []},
        )
        return seen["url"]

    async def test_appends_v1_when_path_empty(self):
        assert await self._forward_url("http://engine.test:8000") == "http://engine.test:8000/v1/chat/completions"

    async def test_keeps_existing_v1(self):
        assert await self._forward_url("http://engine.test:8000/v1") == "http://engine.test:8000/v1/chat/completions"

    async def test_keeps_custom_path(self):
        assert await self._forward_url("http://gw.test/api") == "http://gw.test/api/chat/completions"

    async def test_api_base_path_kept_and_joined(self):
        """api_base 自带版本根保留，path 作为子路径追加；边界斜杠统一补齐单个 /。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(api_base="https://gw.test/v2", path="/test/chat/completion"),
            alias_name="a",
            payload={"model": "a", "messages": []},
        )
        assert seen["url"] == "https://gw.test/v2/test/chat/completion"

    async def test_path_without_leading_slash(self):
        """path 缺前导斜杠时补齐，不产生畸形 host+path 拼接。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        await OpenAIChatAdapter().forward(
            _openai_client(handler),
            _target(api_base="http://engine.test", path="v1/chat/completions"),
            alias_name="a",
            payload={"model": "a", "messages": []},
        )
        assert seen["url"] == "http://engine.test/v1/chat/completions"


class TestResponsePassthrough:
    """响应侧全量透传：vLLM 扩展字段（token_ids/stop_reason/routed_experts）、
    reasoning_content、message/delta/usage 未知字段一律原样转发，不做任何剔除——
    业界网关通行做法，仅 model 回写别名。"""

    def _client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)

    async def test_nonstream_choices_vllm_extensions_passthrough(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "internal",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "hi",
                                "reasoning_content": "思考",
                                "secret_meta": 1,
                            },
                            "token_ids": [1, 2],
                            "stop_reason": "EOS",
                            "routed_experts": {"a": 1},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "engine_usage": 9},
                },
            )

        result = await OpenAIChatAdapter().forward(
            self._client(handler),
            _target(),
            alias_name="a",
            payload={"model": "a", "messages": []},
        )
        choice = result.body["choices"][0]
        # 全量透传：vLLM 扩展字段、message/delta 未知字段、usage 扩展字段原样保留
        assert choice["token_ids"] == [1, 2]
        assert choice["stop_reason"] == "EOS"
        assert choice["routed_experts"] == {"a": 1}
        message = choice["message"]
        assert message["reasoning_content"] == "思考"
        assert message["secret_meta"] == 1
        assert result.body["usage"]["engine_usage"] == 9

    async def test_stream_chunk_vllm_extensions_passthrough(self):
        def handler(request: httpx.Request) -> httpx.Response:
            chunk = {
                "id": "c",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "internal",
                "choices": [
                    {"index": 0, "delta": {"content": "x", "hidden": 1}, "finish_reason": None, "token_ids": [5]}
                ],
            }
            return httpx.Response(
                200,
                content=b"data: " + json.dumps(chunk).encode() + b"\n\n" + b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )

        result = await OpenAIChatAdapter().forward(
            self._client(handler),
            _target(),
            alias_name="a",
            payload={"model": "a", "messages": [], "stream": True},
        )
        chunks = [ev.wire async for ev in result.stream]
        payload = json.loads(chunks[0][len(b"data: ") :].rstrip(b"\n"))
        choice = payload["choices"][0]
        assert choice["token_ids"] == [5]  # vLLM 扩展字段透传
        assert choice["delta"]["hidden"] == 1  # delta 未知字段透传
