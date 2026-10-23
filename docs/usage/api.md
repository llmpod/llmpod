# API 接入

[文档目录](../README.md) · [部署指南](deployment.md) · [开发指南](../development/README.md)

调用前请先完成[模型部署并创建 API Key](../../README.md#快速开始)。

网关提供以下接口，可接入使用相应协议的 SDK 或业务应用：

| 协议 / 能力 | 方法 | 路径 |
| --- | --- | --- |
| OpenAI Chat Completions | `POST` | `/v1/chat/completions` |
| OpenAI Embeddings | `POST` | `/v1/embeddings` |
| OpenAI 模型列表 | `GET` | `/v1/models` |
| Anthropic Messages | `POST` | `/v1/messages` |
| Anthropic Token 计数 | `POST` | `/v1/messages/count_tokens` |
| Rerank | `POST` | `/v1/rerank` |

以下示例使用 OpenAI Chat Completions 接口，连接 Docker 配置模板中的网关端口 `18090`；本地开发网关默认端口为 `8090`。将网关地址和 API Key 替换为实际值，`model` 使用**已发布的模型别名**。

```bash
export LLMPOD_BASE_URL="http://127.0.0.1:18090"
export LLMPOD_API_KEY="<your-api-key>"

# 查看当前 API Key 可访问的已发布模型
curl --fail-with-body "$LLMPOD_BASE_URL/v1/models" \
  -H "Authorization: Bearer $LLMPOD_API_KEY"

# 将 model 替换为上一步返回的模型 id
curl --fail-with-body "$LLMPOD_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $LLMPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-alias>",
    "messages": [{"role": "user", "content": "你好，请介绍一下自己。"}],
    "stream": false
  }'
```

使用 OpenAI 协议的 SDK 时，将 `base_url` 设置为网关地址加 `/v1`，并传入 LLMPod API Key。
