"""计费纯函数：真实费用（compute_cost）与预扣估算（estimate_cost）。

单位：元（Decimal）。定价口径 = 每百万 token（per_mtok）。Route = 单模型别名，
input 价是唯一输入 token 单价（chat 的 prompt、embeddings/rerank 的全部 token）。
估算用启发式：ASCII/4 + 非 ASCII×1（CJK 取 1 为安全高估），completion 兜底
max_tokens 或 4096（o1 等模型不支持 max_tokens，无值时给大兜底防欠扣）。
"""

from __future__ import annotations

import math
from decimal import Decimal

from llmpod.apps.proxyserver.snapshot import RouteEntry
from llmpod.apps.proxyserver.usage_cache import extract_cached_tokens, normalize_anthropic_usage, usage_totals


_MT = Decimal(1_000_000)
_DEFAULT_COMPLETION_EST = 4096


def compute_cost(entry: RouteEntry, operation: str, usage: dict | None) -> Decimal | None:
    """按真实 usage 计算费用（元）。未配置定价或 usage 为空 → None（不计费）。"""
    if not usage:
        return None
    if operation == "anthropic.messages":
        usage = normalize_anthropic_usage(usage)
    if operation in ("chat.completions", "anthropic.messages"):
        return _compute_llm_cost(entry, usage)
    # embeddings / rerank：input-only 操作，全量 token 按输入价计
    if operation in ("embeddings", "rerank"):
        return _input_only_cost(entry.input_price_per_mtok, usage)
    return None


def _compute_llm_cost(entry: RouteEntry, usage: dict) -> Decimal | None:
    if (
        entry.input_price_per_mtok is None
        and entry.output_price_per_mtok is None
        and entry.cached_input_price_per_mtok is None
    ):
        return None
    prompt, completion, _total = usage_totals(usage)
    cached = extract_cached_tokens(usage)
    # 缓存命中部分独立计价：命中按 cached 价，未配置（None）回退全价；未上报全量按输入价
    cached_billed = cached if (cached is not None and entry.cached_input_price_per_mtok is not None) else 0
    uncached_prompt = prompt - cached_billed
    cost = Decimal(0)
    if uncached_prompt > 0 and entry.input_price_per_mtok is not None:
        cost += Decimal(uncached_prompt) / _MT * entry.input_price_per_mtok
    if cached_billed > 0:
        cost += Decimal(cached_billed) / _MT * entry.cached_input_price_per_mtok
    if entry.output_price_per_mtok is not None:
        cost += Decimal(completion) / _MT * entry.output_price_per_mtok
    return cost


def _input_only_cost(price: Decimal | None, usage: dict) -> Decimal | None:
    if price is None:
        return None
    # 优先 prompt/input；只报 total_tokens 的上游（部分 rerank/embeddings）也要计费，
    # 否则计量计 token、计费却为 0（免费调用）
    tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or usage.get("total_tokens") or 0
    return Decimal(tokens) / _MT * price


def estimate_cost(entry: RouteEntry, operation: str, payload: dict | None, est_prompt: int | None = None) -> Decimal:
    """请求前估算费用（预扣用）：粗估 prompt + 预估 completion，偏大防透支。

    ``est_prompt`` 可传入已算好的输入 token 预估（配额判定与预扣共用一次计算）；
    缺省自行计算。"""
    if not payload:
        return Decimal(0)
    est_prompt = est_prompt if est_prompt is not None else estimate_prompt_tokens(payload, operation)
    if operation in ("chat.completions", "anthropic.messages"):
        cost = Decimal(0)
        if entry.input_price_per_mtok is not None:
            cost += Decimal(est_prompt) / _MT * entry.input_price_per_mtok
        est_completion = _extract_max_tokens(payload) or _DEFAULT_COMPLETION_EST
        if entry.output_price_per_mtok is not None:
            cost += Decimal(est_completion) / _MT * entry.output_price_per_mtok
        return cost
    if operation in ("embeddings", "rerank") and entry.input_price_per_mtok is not None:
        return Decimal(est_prompt) / _MT * entry.input_price_per_mtok
    return Decimal(0)


def estimate_prompt_tokens(payload: dict, operation: str) -> int:
    """按操作口径粗估输入 token（配额 precheck 与预扣估算共用同一口径）。"""
    return _text_token_estimate(_extract_input_text(payload, operation))


def estimate_text_tokens(text: str) -> int:
    """按裸文本粗估 token（流式断连时按已交付内容估算 completion 计费用）。"""
    return _text_token_estimate(text)


def _text_token_estimate(text: str) -> int:
    """文本 → token 粗估：ASCII 4 字符≈1 token（isascii 快路径整串判定）；
    非 ASCII（CJK 等）按 1 token 计，宁高勿低。"""
    if not text:
        return 0
    if text.isascii():
        return math.ceil(len(text) / 4)
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    return math.ceil(ascii_chars / 4) + non_ascii


def _extract_max_tokens(payload: dict) -> int | None:
    for key in ("max_tokens", "max_completion_tokens"):
        v = payload.get(key)
        if isinstance(v, int) and v > 0:
            return v
    return None


def _extract_input_text(payload: dict, operation: str) -> str:
    if operation == "embeddings":
        return _input_field_to_text(payload.get("input"))
    if operation == "rerank":
        parts = [payload.get("query") or ""]
        docs = payload.get("documents")
        if isinstance(docs, list):
            parts.extend(str(d) for d in docs)
        return "".join(parts)
    # chat.completions / anthropic.messages：system + messages content。
    # anthropic 的 system 允许字符串或内容块数组两种形态，统一走 _content_to_text。
    parts = [_content_to_text(payload.get("system"))]
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict):
            parts.append(_content_to_text(msg.get("content")))
    return "".join(parts)


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                out.append(part.get("text") or part.get("content") or "")
        return "".join(out)
    return ""


def _input_field_to_text(inp) -> str:
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        return "".join(str(x) for x in inp)
    return ""
