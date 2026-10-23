"""协议层包：抽象基类 + 各协议实现类 + 客户端工厂 + 操作→适配器映射。

新增协议只需：实现 ``ProtocolAdapter`` 子类（forward/get_client/protocol/upstream_errors），
在此 re-export，并登记进 ``ADAPTERS``。
"""

from __future__ import annotations

from llmpod.apps.proxyserver.protocol.anthropic import (
    AnthropicCountTokensAdapter,
    AnthropicMessagesAdapter,
    get_anthropic_client,
)
from llmpod.apps.proxyserver.protocol.base import (
    ForwardResult,
    ProtocolAdapter,
    StreamEvent,
    close_shared_clients,
    collect_passthrough_headers,
    json_payload,
)
from llmpod.apps.proxyserver.protocol.openai import (
    OpenAIChatAdapter,
    OpenAIEmbeddingsAdapter,
    get_openai_client,
)
from llmpod.apps.proxyserver.protocol.rerank import RerankAdapter, get_rerank_http_client

# 操作名 → 适配器实例（与 request_log.operation 同 key；inference_handler 按 operation 索引）
ADAPTERS: dict[str, ProtocolAdapter] = {
    "chat.completions": OpenAIChatAdapter(),
    "embeddings": OpenAIEmbeddingsAdapter(),
    "anthropic.messages": AnthropicMessagesAdapter(),
    "anthropic.count_tokens": AnthropicCountTokensAdapter(),
    "rerank": RerankAdapter(),
}

__all__ = [
    "ForwardResult",
    "ProtocolAdapter",
    "StreamEvent",
    "OpenAIChatAdapter",
    "OpenAIEmbeddingsAdapter",
    "AnthropicMessagesAdapter",
    "AnthropicCountTokensAdapter",
    "RerankAdapter",
    "get_openai_client",
    "get_anthropic_client",
    "get_rerank_http_client",
    "close_shared_clients",
    "collect_passthrough_headers",
    "json_payload",
    "ADAPTERS",
]
