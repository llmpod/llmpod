"""上游模型清单拉取方法单测（httpx.MockTransport 桩，不连真网络）。

锁定四件事：

- 清单地址固定 ``normalize_api_base(api_base) + /models``（裸 host 补 /v1），
  只收连接三元组（protocol / api_base / api_key），没有 path / served_model 可言；
- 鉴权头按协议分派：openai 走网关 ``bearer_headers``，anthropic 用 x-api-key +
  anthropic-version（与 chat 探测共用 ``_ANTHROPIC_VERSION``，此处按字面值锁死防漂移）；
- anthropic 带 ``limit=1000``（默认每页 20 会漏模型），openai 系不带参数；
- 失败（非 2xx / 连不上 / 超时 / 非 JSON / 形状不对 / 体超限）是 422，中文标题带
  探的地址与上游原话；空清单是如实回答返回 []。
"""

from __future__ import annotations

import json

import httpx
import pytest

from llmpod.apps.webserver.services.endpoint_probe import fetch_upstream_models
from llmpod.core.exceptions import LLMPodValidationError

from _endpoint_probe_helpers import install_probe_transport, models_payload, replies, unreachable


def _fetch(*, protocol: str = "openai", api_base: str = "https://api.example.com/v1", api_key: str | None = None):
    return fetch_upstream_models(protocol=protocol, api_base=api_base, api_key=api_key)


async def test_openai_list_sorted_and_deduped(monkeypatch):
    """openai：Bearer 头、不带 query 参数；返回去重排序的 id。"""
    seen = install_probe_transport(monkeypatch, replies(200, models_payload("gpt-4o", "gpt-4o-mini", "gpt-4o")))

    models = await _fetch(api_key="sk-1")

    assert models == ["gpt-4o", "gpt-4o-mini"]
    assert seen[0].url == "https://api.example.com/v1/models"
    assert seen[0].headers["authorization"] == "Bearer sk-1"
    assert str(seen[0].url.params) == ""  # openai 系不带参数：兼容网关对陌生参数可能 400


async def test_bare_host_gets_version_root(monkeypatch):
    """裸 host:port 补版本根（normalize_api_base 同款归一）。"""
    seen = install_probe_transport(monkeypatch, replies(200, models_payload("m")))

    await _fetch(api_base="https://gw:8000")

    assert seen[0].url == "https://gw:8000/v1/models"


async def test_anthropic_headers_and_limit(monkeypatch):
    """anthropic：x-api-key + anthropic-version 头，带 limit=1000（默认每页 20 会漏）。"""
    seen = install_probe_transport(monkeypatch, replies(200, models_payload("claude-3-5")))

    models = await _fetch(protocol="anthropic", api_base="https://api.anthropic.com/v1", api_key="sk-ant")

    assert models == ["claude-3-5"]
    assert seen[0].url == "https://api.anthropic.com/v1/models?limit=1000"
    assert seen[0].headers["x-api-key"] == "sk-ant"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"  # 与 chat 探测同一份，字面值锁死
    assert "authorization" not in seen[0].headers


async def test_no_key_no_auth_headers(monkeypatch):
    """无 Key：两种协议都不带鉴权头（免鉴权清单照常拉）。"""
    seen = install_probe_transport(monkeypatch, replies(200, models_payload("m")))

    await _fetch(api_key=None)
    assert "authorization" not in seen[0].headers

    await _fetch(protocol="anthropic", api_key=None)
    assert "x-api-key" not in seen[1].headers


async def test_empty_list_is_truthful_answer(monkeypatch):
    """上游返回空清单：如实回答 []，不是错误。"""
    install_probe_transport(monkeypatch, replies(200, '{"data": []}'))

    assert await _fetch() == []


async def test_non_2xx_reports_upstream_verbatim(monkeypatch):
    """Key 错/地址错：422 带探的地址、状态码与上游原文。"""
    upstream_error = '{"error":{"message":"Incorrect API key"}}'
    install_probe_transport(monkeypatch, replies(401, upstream_error))

    with pytest.raises(LLMPodValidationError) as ei:
        await _fetch()

    message = str(ei.value)
    assert "https://api.example.com/v1/models" in message
    assert "HTTP 401" in message
    assert upstream_error in message  # 原文透出，不翻译


async def test_connection_failure_reports_exception(monkeypatch):
    """连不上：422 连接失败 + 异常类名（str 可能为空，类名兜底）。"""
    install_probe_transport(monkeypatch, unreachable)

    with pytest.raises(LLMPodValidationError, match="连接失败.*ConnectError"):
        await _fetch()


async def test_timeout_reports_wall_clock(monkeypatch):
    """超时：422 拉取超时，文案带时限（分段超时与墙钟同一口径归超时）。"""

    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    install_probe_transport(monkeypatch, hang)

    with pytest.raises(LLMPodValidationError, match="拉取超时"):
        await _fetch()


async def test_non_json_body_rejected(monkeypatch):
    """2xx 但不是 JSON（打错地址落到 HTML 页）：422，不是静默空清单。"""
    install_probe_transport(monkeypatch, replies(200, "<html>login</html>"))

    with pytest.raises(LLMPodValidationError, match="响应体不是 JSON"):
        await _fetch()


async def test_wrong_shape_rejected(monkeypatch):
    """200 + JSON 但没有 data 数组 / 条目全缺 id：422 形状不对。"""
    install_probe_transport(monkeypatch, replies(200, '{"object": "list"}'))
    with pytest.raises(LLMPodValidationError, match="缺少 data 数组"):
        await _fetch()

    install_probe_transport(monkeypatch, replies(200, '{"data": [{"owned_by": "x"}]}'))
    with pytest.raises(LLMPodValidationError, match="解析不出任何模型 id"):
        await _fetch()


async def test_partial_bad_entries_tolerated(monkeypatch):
    """个别条目缺 id 跳过，不整体拒绝（杂项条目混入的聚合网关）。"""
    body = json.dumps({"data": [{"id": "m1"}, {"owned_by": "x"}, {"id": "m1"}]})
    install_probe_transport(monkeypatch, replies(200, body))

    assert await _fetch() == ["m1"]


async def test_oversized_body_rejected(monkeypatch):
    """体超 1MiB 即断：错配地址指向大 blob 时不当清单解析。"""

    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))

    install_probe_transport(monkeypatch, huge)

    with pytest.raises(LLMPodValidationError, match="不是模型清单的形状"):
        await _fetch()


async def test_unparsable_api_base(monkeypatch):
    """地址无法解析：入参问题 422（同 probe 的口径）。"""
    with pytest.raises(LLMPodValidationError, match="无法解析"):
        await _fetch(api_base="http://[")
