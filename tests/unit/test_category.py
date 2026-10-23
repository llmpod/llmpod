"""category → endpoint path 映射 + 适配器类别/协议声明 单测。"""

from __future__ import annotations

from llmpod.apps.manager.deployment.category import CATEGORY_PATHS, category_to_path
from llmpod.apps.manager.deployment.engine.sglang import SGLangAdapter
from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter


class TestCategoryPath:
    def test_known_categories(self):
        assert category_to_path("llm") == "chat/completions"
        assert category_to_path("embedding") == "embeddings"
        assert category_to_path("rerank") == "rerank"
        assert category_to_path("image") == "images/generations"
        assert category_to_path("stt") == "audio/transcriptions"
        assert category_to_path("tts") == "audio/speech"

    def test_unknown_category_returns_none(self):
        assert category_to_path("nope") is None

    def test_all_known_values_have_path(self):
        # CATEGORY_PATHS 的每个 key 都能解析到非 None
        for cat in CATEGORY_PATHS:
            assert category_to_path(cat) is not None


class TestAnthropicPath:
    def test_anthropic_generate_categories(self):
        assert category_to_path("llm", protocol="anthropic") == "messages"
        assert category_to_path("multimodal", protocol="anthropic") == "messages"

    def test_anthropic_pooling_categories_none(self):
        """embedding/rerank 无 anthropic 面——path=None（调用方不建该协议行）。"""
        assert category_to_path("embedding", protocol="anthropic") is None
        assert category_to_path("rerank", protocol="anthropic") is None
        # 生成类（is_generative 唯一定义处）全部有 anthropic path
        assert category_to_path("llm", protocol="anthropic") is not None
        assert category_to_path("multimodal", protocol="anthropic") is not None

    def test_openai_default_protocol_unchanged(self):
        assert category_to_path("llm") == category_to_path("llm", protocol="openai")


class TestAdapterCategories:
    def test_vllm_supports_llm(self):
        # VLLMAdapter v1 声明支持 llm（embedding 将来 --runner pooling）
        assert "llm" in VLLMAdapter.supported_categories

    def test_supported_categories_is_list_of_str(self):
        assert isinstance(VLLMAdapter.supported_categories, list)
        assert all(isinstance(c, str) for c in VLLMAdapter.supported_categories)


class TestAdapterProtocols:
    def test_generate_model_gets_both_protocols(self):
        """vLLM/SGLang 生成类模型同时暴露 openai + anthropic 面。"""
        for adapter in (VLLMAdapter, SGLangAdapter):
            assert adapter.protocols_for("llm") == ["openai", "anthropic"]
            assert adapter.protocols_for("multimodal") == ["openai", "anthropic"]

    def test_pooling_model_anthropic_stripped(self):
        """embedding/rerank 只有 openai 面（引擎无 anthropic pooling 兼容层）。"""
        for adapter in (VLLMAdapter, SGLangAdapter):
            assert adapter.protocols_for("embedding") == ["openai"]
            assert adapter.protocols_for("rerank") == ["openai"]
