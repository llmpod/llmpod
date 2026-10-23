"""端点探测方法单测（httpx.MockTransport 桩，不连真网络）。

锁定四件事：

- 判据唯一：2xx = 通，其余（非 2xx / 连不上 / 超时）都不通，且上游原文原样透出、不翻译；
- 探测地址与网关同源（``resolve_url``）：登记了主路径走主路径，未登记才归一化接协议缺省路径；
- 闸门三档跳过（全局开关 / ``skip_probe`` / 连接配置未变）各自独立生效，互不牵连；
- 探测不通是 ``LLMPodValidationError``（422），文案里带上游原话与探的地址。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from llmpod.apps.proxyserver.protocol.anthropic import AnthropicMessagesAdapter
from llmpod.apps.proxyserver.protocol.openai import OpenAIChatAdapter
from llmpod.apps.webserver.services import endpoint_probe
from llmpod.apps.webserver.services.endpoint_probe import (
    ProbeTarget,
    ensure_reachable,
    merge_target,
    probe_endpoint,
)
from llmpod.core.exceptions import ErrorCode, LLMPodValidationError

from _endpoint_probe_helpers import (
    endpoint_row,
    install_probe_transport,
    parity_route_target,
    probe_target,
    replies,
    unreachable,
)


def _stored(**overrides) -> ProbeTarget:
    """已存端点的连接配置：探测侧只吃 ``ProbeTarget``，ORM 行由 ``from_stored`` 转一道。"""
    return ProbeTarget.from_stored(endpoint_row(**overrides))


class TestMergeTarget:
    """请求体优先、None 回落已存行（「不传=不改」契约）。"""

    def test_body_fields_win(self):
        target = merge_target(
            stored=_stored(),
            protocol="anthropic",
            api_base="https://gw/anthropic",
            served_model="claude-3-5",
            path="messages",
            api_key="sk-new",
        )

        assert target == probe_target(
            protocol="anthropic",
            api_base="https://gw/anthropic",
            path="messages",
            served_model="claude-3-5",
            api_key="sk-new",
        )

    def test_none_falls_back_to_stored_row(self):
        """编辑弹窗不回传已存 API Key：不回落的探测必然 401，改个名字都被拦。

        注意：换 api_base 而不自带 Key 时**不**回落存储 Key（见
        test_changed_base_does_not_carry_stored_key）——本用例的 api_base 与已存一致。
        """
        target = merge_target(stored=_stored(api_key="sk-stored"), api_base="https://api.example.com/v1")

        assert target.api_key == "sk-stored"
        assert target.served_model == "gpt-4o"
        assert target.api_base == "https://api.example.com/v1"

    def test_changed_base_does_not_carry_stored_key(self):
        """换地址不带走存储 Key：否则任何持 ENDPOINT_UPDATE 的调用方都能把别人的
        Key 引到自己填的地址（Key 全仓唯一读路径就是探测）。宁可探出 401。"""
        target = merge_target(stored=_stored(api_key="sk-stored"), api_base="https://evil.example.com/v1")

        assert target.api_key is None
        assert target.api_base == "https://evil.example.com/v1"

    def test_changed_base_with_new_key_probes_with_it(self):
        """换地址且自带新 Key：正常探（Key 与地址同属本次入参，无外发问题）。"""
        target = merge_target(
            stored=_stored(api_key="sk-stored"), api_base="https://new.example.com/v1", api_key="sk-new"
        )

        assert target.api_key == "sk-new"

    def test_empty_string_key_treated_as_absent(self):
        """客户端把未编辑的空输入序列化成 "" 不罕见："" 掉鉴权头、误判配置变了，
        与 None 在此语义等价，一律回落存储值。"""
        target = merge_target(stored=_stored(api_key="sk-stored"), api_key="")

        assert target.api_key == "sk-stored"

    def test_no_stored_row_keeps_empty(self):
        """接入弹窗（无已存行）：没给的就是没有，不凭空造值。"""
        target = merge_target(protocol="openai", api_base="", served_model="")

        assert target == probe_target(protocol="openai", api_base="", served_model="", api_key=None)

    def test_unchanged_merge_equals_stored(self):
        """同一性判定的地基：什么都没改时，合并结果必须与已存配置相等。"""
        row = _stored()

        assert merge_target(stored=row, api_base=row.api_base, served_model=row.served_model, path=None) == row


class TestProbeEndpoint:
    """真发一次最小 chat：2xx = 通，其余报告上游原文。"""

    async def test_reports_healthy_on_2xx(self, monkeypatch):
        seen = install_probe_transport(monkeypatch, replies(200, '{"choices":[{"message":{"content":"p"}}]}'))

        result = await probe_endpoint(probe_target())

        assert result.healthy is True
        assert result.status_code == 200
        assert result.detail is None
        assert result.url == "https://api.example.com/v1/chat/completions"
        assert result.elapsed_ms >= 0
        # openai 协议不带 max_tokens（o 系/gpt-5 类推理模型会 400 拒收）；ping 提示词、带鉴权
        body = seen[0].read().decode()
        assert '"max_tokens"' not in body and '"model":"gpt-4o"' in body and "ping" in body

    async def test_anthropic_carries_required_max_tokens(self, monkeypatch):
        """anthropic 的 messages API 必填 max_tokens：给最小值省一次完整生成。"""
        seen = install_probe_transport(monkeypatch, replies(200, '{"content":[]}'))

        result = await probe_endpoint(probe_target(protocol="anthropic", api_base="https://api.anthropic.com/v1"))

        assert result.healthy is True
        body = seen[0].read().decode()
        assert '"max_tokens":1' in body
        assert seen[0].headers["x-api-key"] == "sk-1"

    async def test_2xx_with_non_json_body_is_unhealthy(self, monkeypatch):
        """200+HTML（打错端口/网关默认页/SPA 兜底）不算通：网关真实转发要 resp.json()，
        只看状态码就是假健康。"""
        install_probe_transport(monkeypatch, replies(200, "<html>default page</html>"))

        result = await probe_endpoint(probe_target())

        assert result.healthy is False
        assert result.status_code == 200
        assert "default page" in result.detail

    async def test_reports_upstream_error_verbatim(self, monkeypatch):
        payload = '{"error":{"message":"Incorrect API key provided: sk-***","type":"invalid_request_error"}}'
        install_probe_transport(monkeypatch, replies(401, payload))

        result = await probe_endpoint(probe_target(api_key="sk-bad"))

        assert result.healthy is False
        assert result.status_code == 401
        assert result.detail == payload  # 原文，不做任何翻译/归类

    async def test_reports_connection_failure(self, monkeypatch):
        install_probe_transport(monkeypatch, unreachable)

        result = await probe_endpoint(probe_target(api_base="https://nope.invalid/v1", api_key=None))

        assert result.healthy is False
        assert result.status_code is None  # 没到 HTTP 层
        assert result.detail is not None and result.detail.startswith("ConnectError:")
        assert "Name or service not known" in result.detail

    async def test_detail_is_clamped(self, monkeypatch):
        install_probe_transport(monkeypatch, replies(502, "x" * 2000))

        result = await probe_endpoint(probe_target(api_key=None))

        assert result.detail is not None
        assert len(result.detail) == 501  # 500 + 省略号
        assert result.detail.endswith("…")

    async def test_rejects_unknown_protocol(self, monkeypatch):
        """协议未知（如 /test 既不给 endpoint_id 又不给 protocol）：抛 422，不猜 openai。

        猜 openai 会去探错的地址，结论要么假健康、要么假 422 —— 正是本能力要消灭的信号。
        """
        seen = install_probe_transport(monkeypatch, replies(200))

        with pytest.raises(LLMPodValidationError):
            await probe_endpoint(probe_target(protocol=""))

        assert seen == []

    async def test_rejects_unparsable_url(self, monkeypatch):
        """地址无法解析（端口非数字、URL 括坏）→ 422 入参错误，不是 500。"""
        seen = install_probe_transport(monkeypatch, replies(200))

        with pytest.raises(LLMPodValidationError, match="无法解析"):
            await probe_endpoint(probe_target(api_base="http://[::1/v1"))

        assert seen == []

    async def test_reports_wall_clock_timeout(self, monkeypatch):
        """滴答式上游能逐段续命、骗过分段超时；墙钟上限必须收敛它。"""

        async def _slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.5)
            return httpx.Response(200, content=b"{}")

        monkeypatch.setattr(endpoint_probe.settings, "endpoint_probe_timeout", 0.05)
        install_probe_transport(monkeypatch, _slow)

        result = await probe_endpoint(probe_target(api_key=None))

        assert result.healthy is False
        assert result.status_code is None  # 没到 HTTP 层
        assert "超时" in (result.detail or "")

    async def test_oversized_body_not_buffered_and_unhealthy(self, monkeypatch):
        """响应体超字节上限：流式收体即断（不物化整包进内存），按不通报告。
        status_code 为 None——这是我方体量判据不是上游拒绝，/test 写回条件
        （上游真的应答过）不该据此把端点写 down。"""

        def _huge(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * (endpoint_probe._PROBE_BODY_BYTES + 1))

        install_probe_transport(monkeypatch, _huge)

        result = await probe_endpoint(probe_target())

        assert result.healthy is False
        assert result.status_code is None
        assert "不是最小 chat 应答" in (result.detail or "")

    async def test_httpx_segment_timeout_reported_as_timeout(self, monkeypatch):
        """httpx 分段超时（ReadTimeout 等 TimeoutException）与墙钟超时同归「探测超时」，
        不归「连接失败」——标题与细节自相矛盾会把运维引去查 DNS。"""

        async def _stall(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200, content=b"{}")

        monkeypatch.setattr(endpoint_probe.settings, "endpoint_probe_timeout", 0.05)
        install_probe_transport(monkeypatch, _stall)

        result = await probe_endpoint(probe_target(api_key=None))

        assert result.healthy is False
        assert "探测超时" in (result.detail or "")

    async def test_registered_path_wins(self, monkeypatch):
        """登记了主路径：api_base 与 path 拼接即真实流量地址（与网关 resolve_url 同源）。"""
        seen = install_probe_transport(monkeypatch, replies(200))

        result = await probe_endpoint(
            probe_target(api_base="https://gw.example.com/openai", path="chat/completions", api_key=None)
        )

        assert result.url == "https://gw.example.com/openai/chat/completions"
        assert str(seen[0].url) == result.url

    async def test_default_path_by_protocol(self, monkeypatch):
        """未登记主路径：归一化 api_base 后接协议缺省路径。"""
        seen = install_probe_transport(monkeypatch, replies(200))

        openai_result = await probe_endpoint(probe_target(api_base="https://api.example.com", api_key=None))
        anthropic_result = await probe_endpoint(
            probe_target(
                protocol="anthropic", api_base="https://api.anthropic.com", served_model="claude-3-5", api_key="k"
            )
        )

        assert openai_result.url == "https://api.example.com/v1/chat/completions"
        assert anthropic_result.url == "https://api.anthropic.com/v1/messages"
        # anthropic 协议头：x-api-key + version（取自适配器那份，与真实转发同源）
        assert seen[1].headers["x-api-key"] == "k"
        assert seen[1].headers["anthropic-version"] == "2023-06-01"
        assert '"max_tokens":1' in seen[1].read().decode()


class TestEnsureReachable:
    """接入/编辑前置闸门：不通即 422，安静时才不打扰。"""

    async def test_raises_with_upstream_text(self, monkeypatch):
        payload = '{"error":{"message":"Incorrect API key provided"}}'
        install_probe_transport(monkeypatch, replies(401, payload))

        with pytest.raises(LLMPodValidationError) as ei:
            await ensure_reachable(probe_target(api_key="sk-bad"))

        message = str(ei.value)
        assert ei.value.error_code == ErrorCode.VALIDATION_ERROR
        assert "401" in message and payload in message
        assert "https://api.example.com/v1/chat/completions" in message

    async def test_passes_when_reachable(self, monkeypatch):
        install_probe_transport(monkeypatch, replies(200))

        verified = await ensure_reachable(probe_target())

        assert verified is True  # 真探过且通过 → 调用方据此把健康拨回 healthy

    async def test_returns_false_when_skipped(self, monkeypatch):
        """跳过 = 没验证过，返回值不能让调用方误以为「这份配置已被上游验证」。"""
        install_probe_transport(monkeypatch, replies(200))

        assert await ensure_reachable(probe_target(api_key=None), skip_probe=True) is False

    async def test_skips_when_config_unchanged(self, monkeypatch):
        """改个名字 / 启停开关也走 update 接口：连接配置没变就不该插一次上游往返。"""
        seen = install_probe_transport(monkeypatch, replies(500))
        row = _stored()

        await ensure_reachable(merge_target(stored=row, api_base=row.api_base), stored=row)

        assert seen == []

    async def test_probes_when_config_changed(self, monkeypatch):
        seen = install_probe_transport(monkeypatch, replies(200))
        row = _stored()

        await ensure_reachable(merge_target(stored=row, api_key="sk-rotated"), stored=row)

        assert len(seen) == 1
        assert seen[0].headers["authorization"] == "Bearer sk-rotated"

    async def test_skips_when_globally_disabled(self, monkeypatch):
        """容器无外网出口时的兜底：全局一关，所有外部端点仍登记得进来。"""
        seen = install_probe_transport(monkeypatch, replies(500))
        monkeypatch.setattr(endpoint_probe.settings, "endpoint_probe_enabled", False)

        await ensure_reachable(probe_target(api_key=None))

        assert seen == []

    async def test_skips_when_skip_probe(self, monkeypatch):
        seen = install_probe_transport(monkeypatch, replies(500))

        await ensure_reachable(probe_target(api_key=None), skip_probe=True)

        assert seen == []


class TestDataPlaneParity:
    """探测发出的请求必须与真实转发同源（镜像即检测点，口径同 tests/unit/test_constant_drift.py）。

    探测模块手抄了两样网关的东西：协议缺省主路径表、anthropic 鉴权头（那是适配器里的私有
    实现，不为管理端去把它公开化——那是别人的文件）。抄错不会在任何地方报错，只会表现为
    「探不通而流量正常」或反过来，正是本能力要消灭的那类假健康。故守卫让适配器**真的走一遍
    ``forward()``**、拿它实际发出的 URL 与头来比，而不是对着测试里的第三份抄件比。
    """

    @pytest.mark.parametrize("adapter", [OpenAIChatAdapter(), AnthropicMessagesAdapter()], ids=["openai", "anthropic"])
    async def test_probe_request_matches_adapter(self, monkeypatch, adapter):
        for path in (None, "custom/path"):
            cfg = probe_target(protocol=adapter.protocol, path=path, api_key="sk-k")
            seen = install_probe_transport(monkeypatch, replies(200))

            await probe_endpoint(cfg)
            forwarded = await _forward_via_adapter(adapter, cfg)

            assert str(seen[0].url) == str(forwarded.url)
            # 只比适配器**代码里设的**鉴权/协商头：其余（content-length / host / accept-encoding…）
            # 是 httpx 按各自请求体与传输补的，本就不该一致，比了只会误报
            for name in ("content-type", "authorization", "x-api-key", "anthropic-version", "anthropic-beta"):
                assert seen[0].headers.get(name) == forwarded.headers.get(name)


async def _forward_via_adapter(adapter, cfg: ProbeTarget) -> httpx.Request:
    """让适配器真的走一遍 ``forward()``，返回它实际发出的请求。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await adapter.forward(
            client,
            parity_route_target(cfg),
            alias_name="alias",
            payload={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
    return captured[0]
