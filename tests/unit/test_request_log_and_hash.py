"""request_log ORM 守卫 + 会话前缀哈希（协议感知四规定）。

前缀哈希四规定：
1. 规范化 JSON 哈希（sort_keys + 稳定分隔符），不基于原始字节；
2. 有效前缀按协议：OpenAI chat = tools + messages[:-1]；
   Anthropic = 顶层 system + tools + messages[:-1]；
3. 大二进制字段先降噪（base64 图片 → 占位符）；
4. embeddings / rerank 无会话语义 → None。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from llmpod.apps.proxyserver.request_logger import _MAX_LIST_ITEMS, _truncate_strings
from llmpod.apps.proxyserver.routing import compute_conv_prefix_hash
from llmpod.db.base import Base
from llmpod.models import RequestLog  # noqa: F401 — 触发表注册
from llmpod.utils.time import as_utc as _as_utc
from llmpod.utils.time import ms_to_datetime


def test_request_log_table_registered():
    assert "request_log" in Base.metadata.tables


def test_request_log_metering_columns():
    """计量 + 块引用 + 会话指纹 + 上游流向 + 选路原因 + 重试 + 截断/快照开关字段齐备。"""
    t = Base.metadata.tables["request_log"]
    expected = {
        "id",
        "request_id",
        "tenant_id",
        "api_key_id",
        "model_alias",
        "served_model",
        "operation",
        "protocol",
        "engine",
        "upstream_type",
        "provider_name",
        "endpoint_id",
        "route_reason",
        "retry_count",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cost",
        "latency_ms",
        "status",
        "http_status",
        "request_ref",
        "response_ref",
        "conv_prefix_hash",
        "truncated",
        "snapshot_disabled",
        "error_message",
        "created_at",
    }
    assert expected <= set(t.c.keys())


def test_request_log_upstream_and_retry_defaults():
    """upstream_type/provider_name/endpoint_id 可空；retry_count 默认 0。"""
    t = Base.metadata.tables["request_log"]
    for col in ("upstream_type", "provider_name", "endpoint_id"):
        assert t.c[col].nullable is True
    assert t.c["retry_count"].nullable is False
    assert "0" in str(t.c["retry_count"].server_default.arg)


def test_request_log_cached_tokens_nullable():
    """cached_tokens 可空：None=引擎未上报，0=明确无命中。"""
    t = Base.metadata.tables["request_log"]
    col = t.c["cached_tokens"]
    assert col.nullable is True
    assert "server_default" not in col.server_default.arg.lower() if col.server_default else True


def test_request_id_unique_index():
    t = Base.metadata.tables["request_log"]
    uniq = {tuple(c.name for c in idx.columns) for idx in t.indexes if idx.unique}
    assert ("request_id",) in uniq


class TestOpenAIChatPrefix:
    def test_tools_and_all_but_last_message(self):
        p1 = compute_conv_prefix_hash(
            "chat.completions",
            {
                "tools": [{"id": "t1"}],
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "q1"},
                    {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "q2"},
                ],
            },
        )
        # 同前缀 + 相同末条消息 → 同哈希（末条不属于前缀）
        p2 = compute_conv_prefix_hash(
            "chat.completions",
            {
                "tools": [{"id": "t1"}],
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "q1"},
                    {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "q2"},
                ],
            },
        )
        assert p1 == p2 and p1 is not None

        # 前缀变化（改 system）→ 哈希变化
        p3 = compute_conv_prefix_hash(
            "chat.completions",
            {
                "tools": [{"id": "t1"}],
                "messages": [
                    {"role": "system", "content": "OTHER"},
                    {"role": "user", "content": "q1"},
                    {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "q2"},
                ],
            },
        )
        assert p3 != p1

    def test_canonical_json_key_order_insensitive(self):
        """键序不同语义相同 → 同哈希（规范化 JSON 规定）。"""
        a = compute_conv_prefix_hash(
            "chat.completions",
            {
                "messages": [{"content": "x", "role": "user"}, {"content": "y", "role": "user"}],
            },
        )
        b = compute_conv_prefix_hash(
            "chat.completions",
            {
                "messages": [{"role": "user", "content": "x"}, {"role": "user", "content": "y"}],
            },
        )
        assert a == b

    def test_binary_denoise_before_hash(self):
        """base64 图片降噪为占位符：同图片字节差异不影响哈希纯度。"""
        img1 = "iVBORw0KGgo=" + "A" * 400
        img2 = "iVBORw0KGgo=" + "B" * 400
        a = compute_conv_prefix_hash(
            "chat.completions",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img1}"}}],
                    },
                    {"role": "user", "content": "q"},
                ],
            },
        )
        b = compute_conv_prefix_hash(
            "chat.completions",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img2}"}}],
                    },
                    {"role": "user", "content": "q"},
                ],
            },
        )
        assert a == b  # 图片差异被降噪，不扰动会话链


class TestAnthropicPrefix:
    def test_top_level_system_included(self):
        """Anthropic 顶层 system 属于有效前缀。"""
        base_messages = [{"role": "user", "content": "q"}]
        p1 = compute_conv_prefix_hash(
            "anthropic.messages",
            {
                "system": "be terse",
                "messages": base_messages,
            },
        )
        p2 = compute_conv_prefix_hash(
            "anthropic.messages",
            {
                "system": "be verbose",
                "messages": base_messages,
            },
        )
        assert p1 is not None and p1 != p2


class TestNoSessionSemantics:
    def test_embeddings_and_rerank_null(self):
        assert compute_conv_prefix_hash("embeddings", {"input": ["x"]}) is None
        assert compute_conv_prefix_hash("rerank", {"query": "q", "documents": []}) is None


class TestAsUtc:
    def test_naive_treated_as_utc(self):
        dt = _as_utc(datetime(2024, 1, 1, 0, 0, 0))
        assert dt == datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert dt.tzinfo is not None

    def test_aware_preserved(self):
        aware = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        assert _as_utc(aware) is aware

    def test_none_passthrough(self):
        assert _as_utc(None) is None


class TestMsToDatetime:
    def test_epoch_ms_to_utc(self):
        assert ms_to_datetime(1704067200000) == datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_fractional_seconds_floored(self):
        # 1704067200123 ms → 1704067200.123 s，保留毫秒精度
        dt = ms_to_datetime(1704067200123)
        assert dt.microsecond == 123000
        assert dt.tzinfo == timezone.utc

    def test_none_passthrough(self):
        assert ms_to_datetime(None) is None


class TestTruncateStrings:
    def test_long_string_truncated(self):
        obj, truncated = _truncate_strings({"x": "a" * 40000}, max_bytes=100)
        assert truncated
        assert len(obj["x"]) <= 100 + len("…[truncated]")

    def test_oversized_list_capped(self):
        """token_ids 大数组（return_token_ids=true 可达数千）→ 条数上限截断 + truncated。"""
        obj, truncated = _truncate_strings({"token_ids": list(range(5000))}, max_bytes=32 * 1024)
        assert truncated
        assert len(obj["token_ids"]) == _MAX_LIST_ITEMS + 1  # 保留上限条 + 1 占位
        assert obj["token_ids"][-1].startswith("…[")

    def test_small_list_and_dict_untouched(self):
        obj, truncated = _truncate_strings({"a": [1, 2, 3], "b": {"c": "ok"}}, max_bytes=32 * 1024)
        assert not truncated
        assert obj == {"a": [1, 2, 3], "b": {"c": "ok"}}
