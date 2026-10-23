"""协议层 anthropic messages 转发与整形（仅原生直通，不翻译）。

假引擎经 httpx.MockTransport 注入。锁定：请求 model 换 served_model、api_key 走 x-api-key；
非流式 model 回写别名（其余透传）；流式事件透传且 message_start 中的 model 回写别名。
"""

from __future__ import annotations

import json

import httpx
import pytest

from llmpod.apps.proxyserver.protocol import AnthropicMessagesAdapter
from llmpod.apps.proxyserver.snapshot import RouteTarget


def _target(api_key=None) -> RouteTarget:
    return RouteTarget(
        endpoint_id=1,
        deployment_id="dep-1",
        api_base="http://engine.test",
        path="/v1/messages",
        served_model="internal-qwen3",
        engine_type="vllm",
        weight=1,
        priority=0,
        protocol="anthropic",
        api_key=api_key,
    )


def _anthropic_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


def _message_payload(model="internal-qwen3", **ovr) -> dict:
    base = {
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    base.update(ovr)
    return base


def _message_response() -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "internal-qwen3",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7},
        "engine_private": "leak",  # 未知字段 → 透传
    }


class TestNonStreamMessages:
    async def test_model_rewrite_and_passthrough(self):
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_message_response())

        result = await AnthropicMessagesAdapter().forward(
            _anthropic_client(handler),
            _target(),
            alias_name="qwen3-8b",
            payload=_message_payload(model="qwen3-8b"),
        )

        assert seen["body"]["model"] == "internal-qwen3"  # 请求侧换内部名
        assert result.body["model"] == "qwen3-8b"  # 响应侧回写别名
        assert result.body["engine_private"] == "leak"  # 其余字段透传
        assert result.body["usage"]["input_tokens"] == 12

    async def test_non_object_response_passthrough_not_crash(self):
        """200 但顶层非对象（标量）→ 原样透传、不崩、usage=None。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json="scalar")

        result = await AnthropicMessagesAdapter().forward(
            _anthropic_client(handler),
            _target(),
            alias_name="a",
            payload=_message_payload(),
        )
        assert result.body == "scalar"  # 原样透传，不重写 model
        assert result.usage is None

    async def test_api_key_set_as_x_api_key_header(self):
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["key"] = request.headers.get("x-api-key")
            seen["version"] = request.headers.get("anthropic-version")
            return httpx.Response(200, json=_message_response())

        await AnthropicMessagesAdapter().forward(
            _anthropic_client(handler),
            _target(api_key="sk-ant"),
            alias_name="a",
            payload=_message_payload(),
        )
        assert seen["key"] == "sk-ant"
        assert seen["version"] == "2023-06-01"

    async def test_engine_error_raises_http_error(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error"}})

        with pytest.raises(httpx.HTTPStatusError):
            await AnthropicMessagesAdapter().forward(
                _anthropic_client(handler),
                _target(),
                alias_name="a",
                payload=_message_payload(),
            )


class TestStreamMessages:
    async def test_sse_error_is_reported_in_stream_event(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            body = b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error"}}\n\n'
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        result = await AnthropicMessagesAdapter().forward(
            _anthropic_client(handler), _target(), alias_name="a", payload=_message_payload(stream=True)
        )
        events = [event async for event in result.stream]
        assert len(events) == 1
        assert events[0].upstream_error == "overloaded_error"

    async def test_events_passthrough_with_model_rewrite(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_1",
                            "type": "message",
                            "role": "assistant",
                            "model": "internal-qwen3",
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 12, "output_tokens": 1},
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
                ),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 7},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            body = b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events)
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        result = await AnthropicMessagesAdapter().forward(
            _anthropic_client(handler),
            _target(),
            alias_name="qwen3-8b",
            payload=_message_payload(model="qwen3-8b", stream=True),
        )
        parsed_events = [ev async for ev in result.stream]
        out = [ev.wire for ev in parsed_events]

        assert len(out) == 4  # 事件逐条转发

        def _data(sse: bytes) -> dict:
            for line in sse.split(b"\n"):
                if line.startswith(b"data: "):
                    return json.loads(line[len(b"data: ") :])
            raise AssertionError(f"no data line in {sse!r}")

        start = _data(out[0])
        assert start["message"]["model"] == "qwen3-8b"  # message_start 回写别名
        assert parsed_events[0].usage["prompt_tokens"] == 12
        assert "output_tokens" not in parsed_events[0].usage  # 初始 1 token 不是最终用量
        delta = _data(out[1])
        assert delta["delta"]["text"] == "hi"
        mdelta = _data(out[2])
        assert mdelta["usage"]["output_tokens"] == 7  # usage 事件保留

    async def test_base_url_normalized(self):
        """登记端点 api_base 带 /v1 不再重复拼（/v1/v1/messages 是 bug）。"""
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=_message_response())

        target = RouteTarget(
            endpoint_id=2,
            deployment_id="dep-2",
            api_base="https://api.anthropic.com/v1",
            path=None,
            served_model="claude-x",
            engine_type="external",
            weight=1,
            priority=0,
            protocol="anthropic",
        )
        await AnthropicMessagesAdapter().forward(
            _anthropic_client(handler),
            target,
            alias_name="a",
            payload=_message_payload(),
        )
        assert seen["url"] == "https://api.anthropic.com/v1/messages"

    async def test_stream_connection_error_raises_in_forward(self):
        """流式上游 4xx/5xx 在 forward 内同步抛（eager 首事件），failover 循环可捕获。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"type": "error", "error": {}})

        with pytest.raises(httpx.HTTPStatusError):
            await AnthropicMessagesAdapter().forward(
                _anthropic_client(handler),
                _target(),
                alias_name="a",
                payload=_message_payload(stream=True),
            )

    async def test_empty_200_stream_yields_nothing_not_500(self):
        """上游 200 但流为空（anthropic 无尾哨兵）→ 空流，不把 StopAsyncIteration 冒成 500。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

        result = await AnthropicMessagesAdapter().forward(
            _anthropic_client(handler),
            _target(),
            alias_name="a",
            payload=_message_payload(stream=True),
        )
        out = [ev.wire async for ev in result.stream]
        assert out == []
