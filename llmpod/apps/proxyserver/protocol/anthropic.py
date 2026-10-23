"""Anthropic messages 适配器（原生直通，不翻译）。

转发走 httpx 裸调用（宽松解析，容忍引擎兼容层的未知/缺失字段），官方 anthropic SDK
不实例化客户端、不承担网络层。响应全量透传，仅 model 回写别名（别名契约：客户端
看到的 model 必须是其请求过的别名）；流式事件透传，message_start 内嵌 message.model
回写别名，usage/文本增量抽出到 StreamEvent。

客户端的 ``anthropic-version`` / ``anthropic-beta`` 头原样转交上游：版本与 beta 能力
（prompt caching、1M 上下文等）是客户端与上游的协商，网关替客户端定死会让这些能力
静默失效；客户端没带 version 时按官方 SDK 的长期缺省值补。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from llmpod.apps.proxyserver.protocol.base import (
    ForwardResult,
    ProtocolAdapter,
    StreamEvent,
    eager_stream,
    json_payload,
)
from llmpod.apps.proxyserver.usage_cache import normalize_anthropic_usage
from llmpod.apps.proxyserver.snapshot import RouteTarget
from llmpod.core.encoding import lenient_utf8

_ANTHROPIC_VERSION = "2023-06-01"


def _anthropic_headers(api_key: str | None, passthrough: dict[str, str] | None) -> dict:
    """上游鉴权/协商头：客户端带入的 version / beta 原样保留，缺 version 补缺省值。

    自带 content-type（请求体走 ``json_payload`` + ``content=``，httpx 不再自动设置）。
    """
    headers = dict(passthrough or {})
    headers.setdefault("anthropic-version", _ANTHROPIC_VERSION)
    headers["content-type"] = "application/json"
    if api_key:
        headers["x-api-key"] = api_key
    return headers


class AnthropicMessagesAdapter(ProtocolAdapter):
    """anthropic messages 原生直通（不翻译），usage/文本增量在流式逐事件抽出。

    client/URL 策略用基类默认（登记 path 优先，缺省归一化 + /messages）；
    鉴权头是 anthropic 专属（x-api-key + version），本类实现。
    """

    protocol = "anthropic"

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
        url = self._url(target, "/messages")
        headers = _anthropic_headers(target.api_key, passthrough_headers)

        if body.get("stream"):
            return await eager_stream(self._stream_messages(client, url, body, headers, alias_name))

        resp = await client.post(url, content=json_payload(body), headers=headers)
        resp.raise_for_status()
        dumped = resp.json()
        if isinstance(dumped, dict):
            shaped = {**dumped, "model": alias_name}
            usage = normalize_anthropic_usage(shaped.get("usage"))
            finish_reason = shaped.get("stop_reason")
        else:
            # 畸形上游（顶层非对象）：原样透传，不重写 model、不崩溃
            shaped = dumped
            usage = None
            finish_reason = None
        return ForwardResult(body=shaped, usage=usage, finish_reason=finish_reason)

    async def _stream_messages(
        self, client: httpx.AsyncClient, url: str, body: dict, headers: dict, alias_name: str
    ) -> AsyncIterator[StreamEvent]:
        """事件逐条整形：message_start 内嵌 message.model 回写别名；抽出 usage/文本增量。"""
        event_name: str | None = None
        async with client.stream("POST", url, content=json_payload(body), headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    dumped = json.loads(line[len("data:") :].strip())
                    if not isinstance(dumped, dict):
                        # 畸形上游（data 非对象）：原样透传，不解析、不崩溃
                        yield StreamEvent(wire=lenient_utf8(f"data: {json.dumps(dumped, ensure_ascii=False)}\n\n"))
                        continue
                    if dumped.get("type") == "message_start" and isinstance(dumped.get("message"), dict):
                        dumped["message"] = {**dumped["message"], "model": alias_name}

                    etype = dumped.get("type")
                    usage = text = finish_reason = billable_text = upstream_error = None
                    if etype == "message_start":
                        usage = normalize_anthropic_usage((dumped.get("message") or {}).get("usage"))
                        if usage:
                            # message_start.output_tokens 是初始占位，不是最终输出用量。
                            usage.pop("output_tokens", None)
                            usage.pop("total_tokens", None)
                    elif etype == "message_delta":
                        usage = dumped.get("usage")
                        # 收尾事件带 stop_reason（"max_tokens" = 撞到输出上限）
                        finish_reason = (dumped.get("delta") or {}).get("stop_reason")
                    elif etype == "content_block_delta":
                        delta = dumped.get("delta") or {}
                        text = delta.get("text")
                        billable_text = delta.get("partial_json") or delta.get("thinking")
                    elif etype == "error":
                        upstream_error = str((dumped.get("error") or {}).get("type") or "upstream stream error")

                    yield StreamEvent(
                        wire=lenient_utf8(
                            f"event: {event_name or etype or 'event'}\ndata: "
                            f"{json.dumps(dumped, ensure_ascii=False)}\n\n"
                        ),
                        usage=usage,
                        text=text,
                        billable_text=billable_text,
                        upstream_error=upstream_error,
                        finish_reason=finish_reason,
                    )
                    event_name = None


class AnthropicCountTokensAdapter(ProtocolAdapter):
    """/v1/messages/count_tokens 直通（Anthropic SDK 一等公民方法，Claude Code 用它
    做上下文 / 自动压缩判断）。

    model 换 served_model，响应（``{"input_tokens": N}``）透传不整形。token 计数
    不产生 usage / 计费 / 调用日志语义（records_logs=False）；准入失败照常出
    Anthropic 错误信封（protocol = anthropic，模型不存在等由管道归一）。
    """

    protocol = "anthropic"
    records_logs = False

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
        # count_tokens 是 messages 主端点的子端点，经 sub_path 追加：端点登记了主路径
        # 时缺省主路径参数会被忽略（见 resolve_url），并进 default_path 会整段丢掉、
        # 落到主端点上——对 /v1/messages 而言就是真跑一次生成，且此处不落账。
        url = self._url(target, "/messages", sub_path="/count_tokens")
        headers = _anthropic_headers(target.api_key, passthrough_headers)
        resp = await client.post(url, content=json_payload(body), headers=headers)
        resp.raise_for_status()
        return ForwardResult(body=resp.json(), usage=None)


def get_anthropic_client(api_base: str | None = None, api_key: str | None = None) -> httpx.AsyncClient:
    """共享 anthropic 连接池（httpx 按 origin 池化）。"""
    return AnthropicMessagesAdapter.get_client(api_base, api_key)
