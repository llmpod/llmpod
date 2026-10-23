"""rerank 适配器（无官方 SDK，httpx 直通）。

非流式 JSON：model 换 served_model，POST 到 /v1/rerank（path 缺省补 /v1/rerank），
model 回写别名。外部 rerank 供应商（SiliconFlow/Jina/Cohere 等）均走
Authorization: Bearer 鉴权。client/URL/bearer 头均用基类默认。
"""

from __future__ import annotations

import httpx

from llmpod.apps.proxyserver.protocol.base import (
    ForwardResult,
    ProtocolAdapter,
    bearer_headers,
    json_payload,
)
from llmpod.apps.proxyserver.snapshot import RouteTarget


class RerankAdapter(ProtocolAdapter):
    """rerank 直通（无官方 SDK）；落账沿用 openai 标签（响应/错误体同 OpenAI 风格）。"""

    protocol = "openai"

    _auth_headers = staticmethod(bearer_headers)

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
        url = self._url(target, "/rerank")
        resp = await client.post(url, content=json_payload(body), headers=self._auth_headers(target.api_key))
        resp.raise_for_status()
        result = resp.json()
        usage = None
        if isinstance(result, dict):
            result = dict(result)
            result["model"] = alias_name
            usage = result.get("usage")
        return ForwardResult(body=result, usage=usage)


def get_rerank_http_client(api_base: str | None = None, api_key: str | None = None) -> httpx.AsyncClient:
    """rerank 直通用共享 httpx 连接池（无官方 SDK）。"""
    return RerankAdapter.get_client(api_base, api_key)
