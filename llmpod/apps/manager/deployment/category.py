"""模型类别（category）→ 服务端点 path 映射。

``category`` 即 ``Model.model_type`` 字段（字符串，非 enum；已知值见 models/model.py 列注释）。
每个 category 对应一个主服务 path（OpenAI 兼容）：
  - 编排器 ``_finalize`` 据此写 ``ModelEndpoint.path``；
  - API 网关 ``ProxyServer`` 据此暴露端点。

加新类别 = 在 ``CATEGORY_PATHS`` 加一行（不改表结构/迁移）。
"""

from __future__ import annotations

from llmpod.config.enums import ModelType

# category → 主端点子路径（OpenAI 兼容；不含版本根 /v1——版本根由 api_base 携带，
# API 网关 resolve_url 拼 api_base + path）。本地部署 api_base 写 host:port/v1；
# 外部端点由用户把版本根写在 api_base，path 填子路径。
CATEGORY_PATHS: dict[ModelType, str] = {
    ModelType.LLM: "chat/completions",
    ModelType.MULTIMODAL: "chat/completions",  # VLM 走 OpenAI chat（图像输入）
    ModelType.EMBEDDING: "embeddings",
    ModelType.RERANK: "rerank",
    ModelType.IMAGE: "images/generations",
    ModelType.STT: "audio/transcriptions",
    ModelType.TTS: "audio/speech",
}

# anthropic 协议的主端点子路径（messages 挂在 chat serving 上，仅生成类模型
# 可达——类别判定用 ModelType.is_generative 唯一定义处，与 engine.protocols_for 同源）
ANTHROPIC_MESSAGE_PATH = "messages"


def category_to_path(category: str, protocol: str = "openai") -> str | None:
    """category → 主端点 path；未知 category 返回 None（由调用方决定默认/报错）。

    ``ModelType`` 为 ``StrEnum``，成员与字符串等值同 hash，故 ``CATEGORY_PATHS`` 以枚举为键
    但可直接用字符串查表——enum 键只换得字典字面量的拼写期 typo 检查，查表无需枚举转换。
    ``protocol="anthropic"`` 查 anthropic 面 path（仅生成类有值）。
    """
    if protocol == "anthropic":
        return ANTHROPIC_MESSAGE_PATH if ModelType.is_generative(category) else None
    return CATEGORY_PATHS.get(category)
