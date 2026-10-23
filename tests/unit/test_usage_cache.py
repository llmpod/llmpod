"""usage 中缓存命中 token 的提取（cached_tokens 计量列）。

双协议口径：
- OpenAI（含 vLLM）：usage.prompt_tokens_details.cached_tokens；
  顶层历史变体 usage.cached_tokens（vLLM 早期）也认。
- Anthropic：usage.cache_read_input_tokens；写侧 cache_creation_input_tokens
  不属于"命中"，不进 cached_tokens（写侧折扣计价 v1.5 再议）。
- 缺失 → None（未知，与 0 区分；0 明确表示无命中）。

配额/限流扣减口径：cached_tokens 是 prompt_tokens 的子集，总数按引擎上报的
total/prompt 全量计（引擎总 token 已含缓存部分，再扣会双算）。
"""

from __future__ import annotations

from llmpod.apps.proxyserver.usage_cache import extract_cached_tokens, normalize_anthropic_usage


class TestOpenAIStyle:
    def test_prompt_tokens_details(self):
        assert (
            extract_cached_tokens(
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "prompt_tokens_details": {"cached_tokens": 7},
                }
            )
            == 7
        )

    def test_top_level_cached_tokens_fallback(self):
        assert extract_cached_tokens({"prompt_tokens": 10, "cached_tokens": 4}) == 4

    def test_details_take_precedence_over_top_level(self):
        assert (
            extract_cached_tokens(
                {
                    "cached_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 7},
                }
            )
            == 7
        )

    def test_details_without_cached_key(self):
        """prompt_tokens_details 存在但无 cached_tokens（OpenAI 新口径新调用）→ None。"""
        assert (
            extract_cached_tokens(
                {
                    "prompt_tokens": 10,
                    "prompt_tokens_details": {},
                }
            )
            is None
        )

    def test_missing_everything(self):
        assert extract_cached_tokens({"prompt_tokens": 10, "total_tokens": 10}) is None

    def test_none_usage(self):
        assert extract_cached_tokens(None) is None


class TestAnthropicStyle:
    def test_normalizes_all_input_components_without_changing_raw_fields(self):
        raw = {
            "input_tokens": 50,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 200,
            "output_tokens": 10,
        }
        normalized = normalize_anthropic_usage(raw)
        assert normalized["prompt_tokens"] == 1250
        assert normalized["total_tokens"] == 1260
        assert normalized["input_tokens"] == 50
        assert normalize_anthropic_usage(normalized) == normalized
        assert "prompt_tokens" not in raw

    def test_cache_read_input_tokens(self):
        assert (
            extract_cached_tokens(
                {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 6,
                    "cache_creation_input_tokens": 0,
                }
            )
            == 6
        )

    def test_cache_creation_not_counted_as_hit(self):
        """写侧 cache_creation 不属于命中（不计入 cached_tokens）。"""
        assert (
            extract_cached_tokens(
                {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                }
            )
            is None
        )

    def test_zero_cache_read_is_zero(self):
        """显式 0 = 明确无命中（非 None）。"""
        assert extract_cached_tokens({"input_tokens": 5, "cache_read_input_tokens": 0}) == 0


class TestNonDictInputs:
    def test_usage_details_not_dict(self):
        assert extract_cached_tokens({"prompt_tokens_details": "x"}) is None

    def test_cached_tokens_non_int_coerced_or_none(self):
        assert extract_cached_tokens({"prompt_tokens_details": {"cached_tokens": "3"}}) == 3
        assert extract_cached_tokens({"prompt_tokens_details": {"cached_tokens": "abc"}}) is None

    def test_negative_is_none(self):
        assert extract_cached_tokens({"prompt_tokens_details": {"cached_tokens": -1}}) is None
