"""pricing 纯函数测试（compute_cost / estimate_cost）。"""

from __future__ import annotations

from decimal import Decimal

from llmpod.apps.proxyserver.pricing import compute_cost, estimate_cost, estimate_prompt_tokens
from llmpod.apps.proxyserver.snapshot import RouteEntry


def _entry(**kw) -> RouteEntry:
    base = dict(
        route_id=1,
        name="m",
        status="published",
        input_price_per_mtok=Decimal("2"),
        output_price_per_mtok=Decimal("4"),
    )
    base.update(kw)
    return RouteEntry(**base)


class TestComputeCost:
    def test_anthropic_cache_components_are_part_of_prompt(self):
        entry = _entry(
            input_price_per_mtok=Decimal("2"),
            output_price_per_mtok=Decimal("4"),
            cached_input_price_per_mtok=Decimal("0.5"),
        )
        usage = {
            "input_tokens": 50,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 200,
            "output_tokens": 10,
        }
        # 缓存写入按既有输入价；缓存读取按路由配置的缓存价。
        assert compute_cost(entry, "anthropic.messages", usage) == Decimal("0.00224")

    def test_chat_input_output(self):
        entry = _entry(input_price_per_mtok=Decimal("2"), output_price_per_mtok=Decimal("4"))
        usage = {"prompt_tokens": 1000, "completion_tokens": 500}
        assert compute_cost(entry, "chat.completions", usage) == Decimal("0.004")

    def test_chat_cached_differentiated(self):
        entry = _entry(
            input_price_per_mtok=Decimal("2"),
            cached_input_price_per_mtok=Decimal("0.5"),
        )
        usage = {
            "prompt_tokens": 1000,
            "prompt_tokens_details": {"cached_tokens": 400},
            "completion_tokens": 0,
        }
        # uncached=600 → 600/1e6*2=0.0012；cached=400 → 400/1e6*0.5=0.0002；合计 0.0014
        assert compute_cost(entry, "chat.completions", usage) == Decimal("0.0014")

    def test_embeddings(self):
        """embeddings 复用 input 价（单模型定价：输入 token 单价唯一）。"""
        entry = _entry(input_price_per_mtok=Decimal("1.5"))
        usage = {"prompt_tokens": 1000}
        assert compute_cost(entry, "embeddings", usage) == Decimal("0.0015")

    def test_rerank(self):
        """rerank 同 embeddings：全量 token 按 input 价。"""
        entry = _entry(input_price_per_mtok=Decimal("1"))
        usage = {"prompt_tokens": 2000}
        assert compute_cost(entry, "rerank", usage) == Decimal("0.002")

    def test_no_price_returns_none(self):
        entry = _entry(input_price_per_mtok=None, output_price_per_mtok=None)
        usage = {"prompt_tokens": 1000}
        assert compute_cost(entry, "embeddings", usage) is None

    def test_empty_usage_returns_none(self):
        entry = _entry()
        assert compute_cost(entry, "chat.completions", None) is None

    def test_chat_no_prices_returns_none(self):
        entry = _entry(
            input_price_per_mtok=None,
            output_price_per_mtok=None,
            cached_input_price_per_mtok=None,
        )
        usage = {"prompt_tokens": 1000, "completion_tokens": 500}
        assert compute_cost(entry, "chat.completions", usage) is None


class TestEstimateCost:
    def test_cjk_prompt_estimate(self):
        # 1000 汉字：len//4=250 是错的，应按非 ASCII ×1 ≈ 1000
        entry = _entry(input_price_per_mtok=Decimal("2"), output_price_per_mtok=Decimal("4"))
        payload = {"messages": [{"role": "user", "content": "汉" * 1000}]}
        est = estimate_cost(entry, "chat.completions", payload)
        expected = Decimal("1000") / 1_000_000 * Decimal("2") + Decimal("4096") / 1_000_000 * Decimal("4")
        assert est == expected

    def test_anthropic_block_system_estimate(self):
        """anthropic 官方 system 允许内容块数组形态——不应 TypeError。"""
        entry = _entry(input_price_per_mtok=Decimal("2"), output_price_per_mtok=Decimal("0"))
        payload = {
            "system": [{"type": "text", "text": "you are helpful"}],  # 15 ascii → 4
            "messages": [{"role": "user", "content": "hi"}],  # 2 ascii → 1
        }
        assert estimate_cost(entry, "anthropic.messages", payload) == Decimal("5") / 1_000_000 * Decimal("2")

    def test_anthropic_string_system_estimate(self):
        entry = _entry(input_price_per_mtok=Decimal("2"), output_price_per_mtok=Decimal("0"))
        payload = {"system": "you are helpful", "messages": [{"role": "user", "content": "hi"}]}
        assert estimate_cost(entry, "anthropic.messages", payload) == Decimal("5") / 1_000_000 * Decimal("2")

    def test_estimate_prompt_tokens_shared(self):
        """estimate_prompt_tokens 供配额 precheck 复用（与预扣同口径）。"""
        assert estimate_prompt_tokens({"messages": [{"role": "u", "content": "汉" * 100}]}, "chat.completions") == 100

    def test_ascii_prompt_estimate(self):
        entry = _entry(input_price_per_mtok=Decimal("2"), output_price_per_mtok=Decimal("0"))
        payload = {"messages": [{"role": "user", "content": "hello world"}]}  # 11 ascii 字符
        # est_prompt = ceil(11/4) = 3，completion 价 0 → 仅输入
        assert estimate_cost(entry, "chat.completions", payload) == Decimal("3") / 1_000_000 * Decimal("2")

    def test_max_tokens_output_estimate(self):
        entry = _entry(input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("4"))
        payload = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        # est_completion = max_tokens = 100
        assert estimate_cost(entry, "chat.completions", payload) == Decimal("100") / 1_000_000 * Decimal("4")

    def test_embeddings_estimate(self):
        """embeddings 预扣估算同样走 input 价。"""
        entry = _entry(input_price_per_mtok=Decimal("1.5"), output_price_per_mtok=None)
        payload = {"input": "汉" * 500}
        assert estimate_cost(entry, "embeddings", payload) == Decimal("500") / 1_000_000 * Decimal("1.5")

    def test_rerank_estimate(self):
        entry = _entry(input_price_per_mtok=Decimal("1"), output_price_per_mtok=None)
        payload = {"query": "汉" * 100, "documents": ["汉" * 400]}
        assert estimate_cost(entry, "rerank", payload) == Decimal("500") / 1_000_000 * Decimal("1")
