"""usage 口径归一：缓存命中 token 提取 + prompt/completion/total 三段抽取。

口径：OpenAI = prompt_tokens_details.cached_tokens（顶层 cached_tokens 兜底）；
Anthropic = cache_read_input_tokens；缺失 → None（未知）与 0（明确无命中）区分。
token 三段以 prompt_tokens/input_tokens 同义对取值，total 缺失按两段之和补。
"""

from __future__ import annotations


def _as_int(v) -> int | None:
    """宽松转 int：bool 排除、数值字符串可转、其余 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return None
    return None


def usage_totals(usage: dict | None) -> tuple[int, int, int]:
    """从 usage dict 抽 (prompt, completion, total)；三处消费方（落账/指标）共用本口径。

    openai 与 anthropic 字段名同义对取值，total 缺失按 prompt+completion 补。
    """
    usage = usage or {}
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total = usage.get("total_tokens") or (prompt + completion)
    return prompt, completion, total


def extract_cached_tokens(usage: dict | None) -> int | None:
    """从 usage dict 提取缓存命中 token 数；引擎未上报 → None。"""
    if not isinstance(usage, dict):
        return None

    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and "cached_tokens" in details:
        v = _as_int(details["cached_tokens"])
        if v is not None and v >= 0:
            return v
        return None

    if "cached_tokens" in usage:
        v = _as_int(usage["cached_tokens"])
        if v is not None and v >= 0:
            return v
        return None

    if "cache_read_input_tokens" in usage:
        v = _as_int(usage["cache_read_input_tokens"])
        if v is not None and v >= 0:
            return v
        return None

    return None


def normalize_anthropic_usage(usage: dict | None) -> dict | None:
    """保留 Anthropic 原字段，同时补齐网关使用的全量输入/总 token 口径。

    Anthropic 的 input_tokens 只含未缓存部分；缓存写入和读取是另外两项。
    这个函数可重复调用，prompt_tokens 始终从原始三项计算。
    """
    if not isinstance(usage, dict):
        return usage
    result = dict(usage)
    if "input_tokens" in result:
        result["prompt_tokens"] = sum(
            max(0, _as_int(result.get(name)) or 0)
            for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
    if "prompt_tokens" in result and "output_tokens" in result:
        # output_tokens 与输入项同样过 _as_int：上游偶发报数值字符串时不致 TypeError
        result["total_tokens"] = result["prompt_tokens"] + (_as_int(result.get("output_tokens")) or 0)
    return result
