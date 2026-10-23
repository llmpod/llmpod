"""OpenAI 系协议适配器：chat.completions 与 embeddings。

转发走 httpx 裸调用（宽松解析，容忍引擎兼容层的未知/缺失字段），官方 openai SDK
不实例化客户端、不承担网络层。响应全量透传，仅 model 回写别名——别名契约：客户端
看到的 model 必须是其请求过的别名，漏出 served_model 会断回填链路。官方 SDK 对未知
字段 extra="allow"，按 SDK 字段集白名单会静默吞掉上游新字段（思考模型的
reasoning_content、vLLM 扩展字段 token_ids/stop_reason 等），故不做任何剔除。
chat 额外处理流式 SSE；embeddings 非流式。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from llmpod.apps.proxyserver.protocol.base import (
    ForwardResult,
    ProtocolAdapter,
    StreamEvent,
    bearer_headers,
    eager_stream,
    json_payload,
)
from llmpod.apps.proxyserver.snapshot import RouteTarget
from llmpod.core.encoding import lenient_utf8


class OpenAIAdapterBase(ProtocolAdapter):
    """openai SDK 家族共用：透传 + model 回写别名（client/URL/bearer 头均用基类默认）。"""

    protocol = "openai"

    _auth_headers = staticmethod(bearer_headers)

    def _shape_top_level(self, dumped: Any, alias_name: str) -> Any:
        """透传，仅 model 回写别名；非对象响应（标量/数组）原样透传不重写、不崩溃。"""
        if not isinstance(dumped, dict):
            return dumped
        return {**dumped, "model": alias_name}


def _chat_finish_reason(shaped: Any) -> str | None:
    """chat.completions 响应的结束原因（第一个带值的 choice）。

    收尾 chunk 才有值（``"length"`` = 撞到 max_tokens，``"stop"`` = 自然结束），
    中间帧是 null——调用方按「最后一个非空」收口。
    """
    if not isinstance(shaped, dict):
        return None
    for choice in shaped.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason"):
            return choice["finish_reason"]
    return None


class OpenAIChatAdapter(OpenAIAdapterBase):
    """chat.completions 适配：全量透传 + model 回写别名 + 流式 SSE。"""

    async def forward(
        self,
        client: httpx.AsyncClient,
        target: RouteTarget,
        *,
        alias_name: str,
        payload: dict,
        passthrough_headers: dict[str, str] | None = None,
    ) -> ForwardResult:
        body = dict(payload)
        body["model"] = target.served_model
        if body.get("stream"):
            body["stream_options"] = {"include_usage": True}  # 末 chunk 带 usage
        url = self._url(target, "/chat/completions")
        headers = self._auth_headers(target.api_key)

        if body.get("stream"):
            return await eager_stream(self._stream_chat(client, url, body, headers, alias_name))

        resp = await client.post(url, content=json_payload(body), headers=headers)
        resp.raise_for_status()
        shaped = self._shape_top_level(resp.json(), alias_name)
        usage = shaped.get("usage") if isinstance(shaped, dict) else None
        return ForwardResult(body=shaped, usage=usage, finish_reason=_chat_finish_reason(shaped))

    async def _stream_chat(
        self, client: httpx.AsyncClient, url: str, body: dict, headers: dict, alias_name: str
    ) -> AsyncIterator[StreamEvent]:
        """流式 SSE 逐 chunk 整形 → StreamEvent（model 回写别名），[DONE] 收尾。"""
        async with client.stream("POST", url, content=json_payload(body), headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    # 畸形上游（data 非对象）：原样透传，不重写 model、不崩溃
                    yield StreamEvent(wire=lenient_utf8(f"data: {json.dumps(parsed, ensure_ascii=False)}\n\n"))
                    continue
                shaped = self._shape_top_level(parsed, alias_name)
                parts = [(ch.get("delta") or {}).get("content") for ch in shaped.get("choices") or []]
                parts = [p for p in parts if p]
                extra_parts = []
                for choice in shaped.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        extra_parts.append(reasoning)
                    if (delta.get("function_call") or {}).get("arguments"):
                        extra_parts.append(delta["function_call"]["arguments"])
                    for tool in delta.get("tool_calls") or []:
                        arguments = (tool.get("function") or {}).get("arguments")
                        if arguments:
                            extra_parts.append(arguments)
                yield StreamEvent(
                    wire=lenient_utf8(f"data: {json.dumps(shaped, ensure_ascii=False)}\n\n"),
                    usage=shaped.get("usage"),
                    text="".join(parts) if parts else None,
                    billable_text="".join(extra_parts) if extra_parts else None,
                    finish_reason=_chat_finish_reason(shaped),
                )
        yield StreamEvent(wire=b"data: [DONE]\n\n")


class OpenAIEmbeddingsAdapter(OpenAIAdapterBase):
    """embeddings 适配：非流式，model 回写 + 透传；计费与落账照常（有真实 usage）。"""

    async def forward(
        self,
        client: httpx.AsyncClient,
        target: RouteTarget,
        *,
        alias_name: str,
        payload: dict,
        passthrough_headers: dict[str, str] | None = None,
    ) -> ForwardResult:
        body = dict(payload)
        body["model"] = target.served_model
        url = self._url(target, "/embeddings")
        resp = await client.post(url, content=json_payload(body), headers=self._auth_headers(target.api_key))
        resp.raise_for_status()
        shaped = self._shape_top_level(resp.json(), alias_name)
        usage = shaped.get("usage") if isinstance(shaped, dict) else None
        return ForwardResult(body=shaped, usage=usage)


def get_openai_client(api_base: str | None = None, api_key: str | None = None) -> httpx.AsyncClient:
    """共享 openai 连接池（httpx 按 origin 池化）。"""
    return OpenAIAdapterBase.get_client(api_base, api_key)
