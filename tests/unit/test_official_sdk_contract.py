"""官方协议 SDK 契约测试（API 网关协议层依赖的六个行为断言）。

升级门禁五项 + 流式行为：
openai / anthropic SDK 升级前必须全绿，防止以下契约被上游破坏：
  1. 自定义 base_url 指向引擎端点可用
  2. extra_body 未知参数原样透传给引擎（客户端新参数不因 SDK 版本丢失）
  3. 响应/流式 chunk 中的引擎私有字段被保留（extra="allow"，整形层全量透传，仅 model 回写别名）
  4. usage 可提取（OpenAI：响应体/末 chunk；Anthropic：message_start + message_delta）
  5. 引擎错误映射为 typed 异常（错误归一的基础）
  6. 流式 SSE 解析与重序列化往返有效（整形转发的实现基础）

全部基于 httpx.MockTransport 内存假引擎，无网络、无真实引擎依赖。
"""

from __future__ import annotations

import json

import httpx
import pytest

# anthropic 1.0.0 自带 vendored fork（httpx2），未随主依赖声明——缺装时跳过而非收集报错；
# SDK 导入放在 skip 判定之后（httpx2 缺失时 anthropic 也 import 不了）
httpx2 = pytest.importorskip("httpx2")
from anthropic import AsyncAnthropic  # noqa: E402 — 须在 importorskip 之后
from openai import AsyncOpenAI  # noqa: E402 — 同上

# 注意：anthropic 1.0.0 拒绝注入标准 httpx.AsyncClient，要求其自带的 httpx2（vendored fork 2.12.0）；
# openai SDK 用标准 httpx。两个 HTTP 栈在同一进程共存无冲突，仅注入 mock 时需各自使用对应模块。


def _openai_client(handler) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="http://engine.test/v1",
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10),
    )


def _anthropic_client(handler) -> AsyncAnthropic:
    return AsyncAnthropic(
        base_url="http://engine.test",
        api_key="test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler), timeout=10),
    )


def _sse(events: list[dict], with_done: bool = False) -> bytes:
    """把事件列表编码为 OpenAI 风格 SSE 字节流（data: 行）。"""
    out = b"".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode() for e in events)
    return out + (b"data: [DONE]\n\n" if with_done else b"")


def _anthropic_sse(typed_events: list[tuple[str, dict]]) -> bytes:
    """把 (event, payload) 列表编码为 Anthropic 风格 SSE 字节流（event: + data: 行）。"""
    out = b""
    for name, payload in typed_events:
        out += f"event: {name}\n".encode()
        out += f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    return out


class TestOpenAISDKContract:
    async def test_nonstream_usage_and_extra_fields_preserved(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "qwen-72b-internal",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    "vllm_private": {"cache_hit": 7},  # 引擎私有字段
                },
            )

        client = _openai_client(handler)
        resp = await client.chat.completions.create(
            model="qwen-72b-internal",  # 网关以 served_model 调引擎（别名改写发生在整形层）
            messages=[{"role": "user", "content": "ping"}],
        )
        assert resp.usage.prompt_tokens == 10 and resp.usage.completion_tokens == 5
        dumped = resp.model_dump()
        assert dumped["vllm_private"] == {"cache_hit": 7}, "引擎私有字段应被保留（整形层全量透传）"
        assert captured["url"].endswith("/v1/chat/completions")
        assert captured["body"]["model"] == "qwen-72b-internal"

    async def test_stream_usage_and_extra_fields_preserved(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body.get("stream") is True
            assert body.get("stream_options") == {"include_usage": True}
            events = [
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "m",
                    "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}],
                },
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "m",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "vllm_private": "x",
                },
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "m",
                    "choices": [],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ]
            return httpx.Response(
                200,
                content=_sse(events, with_done=True),
                headers={"content-type": "text/event-stream"},
            )

        client = _openai_client(handler)
        stream = await client.chat.completions.create(
            model="m",
            messages=[{"role": "user", "content": "x"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        texts, usage, extra_seen, reserialized = [], None, False, []
        async for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            if getattr(chunk, "vllm_private", None):
                extra_seen = True
            if chunk.choices:
                texts.append(chunk.choices[0].delta.content or "")
            reserialized.append(chunk.model_dump_json(exclude_none=True))
        assert "".join(texts) == "你好"
        assert usage is not None and usage.prompt_tokens == 10, "末 chunk 必须带 usage（计费来源）"
        assert extra_seen, "流式 chunk 的引擎私有字段应被保留"
        for r in reserialized:
            json.loads(r)  # 重序列化产物必须是合法 JSON（整形转发基础）

    async def test_extra_body_unknown_params_forwarded(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "m",
                    "choices": [{"index": 0, "message": {"role": "a", "content": "x"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        client = _openai_client(handler)
        await client.chat.completions.create(
            model="m",
            messages=[{"role": "user", "content": "x"}],
            extra_body={"future_engine_param": 42},
        )
        assert captured["body"]["future_engine_param"] == 42, "未知参数必须透传引擎"

    async def test_error_typed_exception(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={
                    "error": {"message": "engine busy", "type": "rate_limit_error", "code": "429"},
                },
            )

        import openai

        client = _openai_client(handler)
        with pytest.raises(openai.RateLimitError):
            await client.chat.completions.create(
                model="m",
                messages=[{"role": "user", "content": "x"}],
            )


class TestAnthropicSDKContract:
    async def test_nonstream_usage_and_extra_fields_preserved(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Request:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx2.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "m",
                    "content": [{"type": "text", "text": "hi"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 7},
                    "engine_private": "e",
                },
            )

        client = _anthropic_client(handler)
        resp = await client.messages.create(
            model="m",
            max_tokens=32,
            messages=[{"role": "user", "content": "ping"}],
        )
        assert resp.usage.input_tokens == 12 and resp.usage.output_tokens == 7
        assert resp.model_dump().get("engine_private") == "e"
        assert captured["url"].endswith("/v1/messages")

    async def test_stream_usage_from_events_and_extra_body(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_1",
                            "type": "message",
                            "role": "assistant",
                            "model": "m",
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 12, "output_tokens": 1},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你好"}},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            return httpx2.Response(
                200,
                content=_anthropic_sse(events),
                headers={"content-type": "text/event-stream"},
            )

        client = _anthropic_client(handler)
        stream = await client.messages.create(
            model="m",
            max_tokens=32,
            messages=[{"role": "user", "content": "x"}],
            stream=True,
            extra_body={"future_engine_param": 42},
        )
        input_tokens = output_tokens = None
        texts = []
        async for event in stream:
            etype = getattr(event, "type", None)
            if etype == "message_start":
                input_tokens = event.message.usage.input_tokens
            elif etype == "content_block_delta":
                texts.append(event.delta.text)
            elif etype == "message_delta":
                output_tokens = event.usage.output_tokens
        assert "".join(texts) == "你好"
        assert input_tokens == 12 and output_tokens == 7, (
            "Anthropic usage 必须取自 message_start + message_delta（计费来源）"
        )
        assert captured["body"]["future_engine_param"] == 42, "未知参数必须透传引擎"
        assert captured["body"]["stream"] is True

    async def test_error_typed_exception(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx2.Response(
                429,
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "engine busy"},
                },
            )

        import anthropic

        client = _anthropic_client(handler)
        with pytest.raises(anthropic.RateLimitError):
            await client.messages.create(
                model="m",
                max_tokens=32,
                messages=[{"role": "user", "content": "x"}],
            )
