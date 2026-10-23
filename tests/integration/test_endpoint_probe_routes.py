"""来源端点探测的路由级集成测试（stub get_db + httpx.MockTransport，不连真网络/PG）。

锁定三条契约：

- ``POST /admin/endpoints/test``：结论恒 200（报告结论，不把上游拒绝做成 4xx），
  上游原文原样回传；只有入参不足 422 / 端点不存在 404 / 本地部署端点 422 三种真错误；
- 接入与编辑的闸门：探测不通 → 422 且**不落库**（行未被触碰）；跳过档（全局开关 /
  ``skip_probe`` / 连接配置未变）各自生效；
- 健康写回只在「显式指定端点 + 探的正是该行存储配置」时发生，改了配置再测只报告。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.config.settings import settings
from llmpod.core.exceptions import ErrorCode
from llmpod.db import get_db
from llmpod.models import ModelEndpoint

from _endpoint_probe_helpers import endpoint_row, install_probe_transport, replies, unreachable
from _fakes import admin_session
from _op_session import OpSession

BASE = "/api/v1/admin/endpoints"

_OK_BODY = '{"choices":[]}'
_UPSTREAM_ERROR = '{"error":{"message":"Incorrect API key provided: sk-***"}}'


@pytest_asyncio.fixture
async def session():
    """桩会话 + 超管会话（权限短路，不触库）。

    用 ``OpSession``（本目录新增用例的共用增强桩）：它按列的 server_default 回填
    created_at / updated_at 等，等价于真实 INSERT 后 RETURNING 回读。
    """

    async def _no_audit(_entry):
        return None

    monkeypatch_fixture = pytest.MonkeyPatch()
    monkeypatch_fixture.setattr("llmpod.services.audit_service._write_audit", _no_audit)
    s = OpSession()

    async def _override_db():
        yield s

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: admin_session()
    yield s
    app.dependency_overrides.clear()
    monkeypatch_fixture.undo()


class TestProbeRoute:
    """``POST /admin/endpoints/test``：报告结论，不以 4xx 表达上游拒绝。"""

    async def test_reports_healthy_from_body_only(self, client: AsyncClient, session, monkeypatch):
        """接入弹窗先测一下：不传 endpoint_id，全靠本次入参。"""
        seen = install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        response = await client.post(
            f"{BASE}/test",
            json={
                "protocol": "openai",
                "api_base": "https://api.openai.com/v1",
                "served_model": "gpt-4o",
                "api_key": "sk-live",
            },
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["healthy"] is True
        assert data["url"] == "https://api.openai.com/v1/chat/completions"
        assert data["status_code"] == 200
        assert data["detail"] is None
        assert seen[0].headers["authorization"] == "Bearer sk-live"

    async def test_reports_upstream_error_verbatim(self, client: AsyncClient, session, monkeypatch):
        install_probe_transport(monkeypatch, replies(401, _UPSTREAM_ERROR))

        response = await client.post(
            f"{BASE}/test",
            json={"protocol": "openai", "api_base": "https://api.example.com/v1", "served_model": "gpt-4o"},
        )

        assert response.status_code == 200  # 上游拒绝 ≠ 本次请求写错
        data = response.json()["data"]
        assert data["healthy"] is False
        assert data["status_code"] == 401
        assert data["detail"] == _UPSTREAM_ERROR  # 原文透出，不做分类/翻译

    async def test_falls_back_to_stored_row(self, client: AsyncClient, session, monkeypatch):
        """编辑弹窗拿不到已存 Key：地址不动、只换模型名时，Key 与地址都用库里那份。"""
        session.preload(endpoint_row(api_key="sk-stored", served_model="gpt-4o"))
        seen = install_probe_transport(monkeypatch, replies(200))

        response = await client.post(f"{BASE}/test", json={"endpoint_id": 7, "served_model": "gpt-4o-mini"})

        assert response.status_code == 200
        assert response.json()["data"]["endpoint_id"] == 7  # 审计 resource_id 的来源字段
        assert response.json()["data"]["url"] == "https://api.example.com/v1/chat/completions"
        assert seen[0].headers["authorization"] == "Bearer sk-stored"
        assert '"model":"gpt-4o-mini"' in seen[0].read().decode()

    async def test_changed_base_does_not_carry_stored_key(self, client: AsyncClient, session, monkeypatch):
        """换地址不带走存储 Key（防 Key 外发）：探测无凭据照发，结论仍真实。"""
        session.preload(endpoint_row(api_key="sk-stored"))
        seen = install_probe_transport(monkeypatch, replies(401, _UPSTREAM_ERROR))

        response = await client.post(f"{BASE}/test", json={"endpoint_id": 7, "api_base": "https://evil.example.com/v1"})

        assert response.status_code == 200
        assert response.json()["data"]["url"] == "https://evil.example.com/v1/chat/completions"
        assert "authorization" not in seen[0].headers

    async def test_rejects_local_endpoint(self, client: AsyncClient, session, monkeypatch):
        """本地部署端点的健康归部署投影：探了也会被覆盖，直接拒。"""
        session.preload(endpoint_row(deployment_id="dep-1", engine_type="vllm"))
        seen = install_probe_transport(monkeypatch, replies(200))

        response = await client.post(f"{BASE}/test", json={"endpoint_id": 7})

        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value
        assert seen == []

    async def test_rejects_missing_input(self, client: AsyncClient, session, monkeypatch):
        seen = install_probe_transport(monkeypatch, replies(200))

        response = await client.post(f"{BASE}/test", json={})

        assert response.status_code == 422
        assert seen == []

    async def test_rejects_missing_protocol(self, client: AsyncClient, session, monkeypatch):
        """不传 endpoint_id 又不给 protocol：不能静默按 openai 探（会探错地址、给假结论）。"""
        seen = install_probe_transport(monkeypatch, replies(200))

        response = await client.post(
            f"{BASE}/test", json={"api_base": "https://api.example.com/v1", "served_model": "m"}
        )

        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value
        assert seen == []

    async def test_unknown_endpoint_is_404(self, client: AsyncClient, session, monkeypatch):
        install_probe_transport(monkeypatch, replies(200))

        response = await client.post(f"{BASE}/test", json={"endpoint_id": 404})

        assert response.status_code == 404
        assert response.json()["error_code"] == ErrorCode.NOT_FOUND.value

    async def test_writes_health_back_when_config_unchanged(self, client: AsyncClient, session, monkeypatch):
        """Key 被上游吊销后的发现入口：探的就是库里那份配置 → 结论写回。"""
        row = session.preload(endpoint_row(health_status="healthy"))
        install_probe_transport(monkeypatch, replies(401, _UPSTREAM_ERROR))

        response = await client.post(f"{BASE}/test", json={"endpoint_id": 7})

        assert response.status_code == 200
        assert response.json()["data"]["healthy"] is False
        assert row.health_status == "down"

    async def test_writes_health_back_on_recovery(self, client: AsyncClient, session, monkeypatch):
        row = session.preload(endpoint_row(health_status="down"))
        install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        await client.post(f"{BASE}/test", json={"endpoint_id": 7})

        assert row.health_status == "healthy"

    async def test_no_write_back_when_config_changed(self, client: AsyncClient, session, monkeypatch):
        """编辑弹窗里试填新 Key 再测：结论不属于该行现状，只报告不写回。"""
        row = session.preload(endpoint_row(health_status="healthy"))
        install_probe_transport(monkeypatch, replies(401, _UPSTREAM_ERROR))

        response = await client.post(f"{BASE}/test", json={"endpoint_id": 7, "api_key": "sk-typo"})

        assert response.status_code == 200
        assert response.json()["data"]["healthy"] is False
        assert row.health_status == "healthy"

    async def test_no_write_back_on_connection_failure(self, client: AsyncClient, session, monkeypatch):
        """上游未应答（连接失败/超时）不写 down：本仓没有后台复探自愈，一次网络抖动
        不该把 healthy 行踢出候选池——结论留在响应里即可。"""
        row = session.preload(endpoint_row(health_status="healthy"))
        install_probe_transport(monkeypatch, unreachable)

        response = await client.post(f"{BASE}/test", json={"endpoint_id": 7})

        assert response.status_code == 200
        assert response.json()["data"]["healthy"] is False
        assert row.health_status == "healthy"

    async def test_no_write_back_on_degraded_row(self, client: AsyncClient, session, monkeypatch):
        """人工标的 degraded 不被诊断点击翻转（与 update 路由同规矩：degraded 只能人工改）。"""
        row = session.preload(endpoint_row(health_status="degraded"))
        install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        response = await client.post(f"{BASE}/test", json={"endpoint_id": 7})

        assert response.status_code == 200
        assert response.json()["data"]["healthy"] is True
        assert row.health_status == "degraded"

    async def test_requires_auth(self, client: AsyncClient):
        """无 token → 401（证明路由已注册且受 require_permission 保护）。"""
        response = await client.post(f"{BASE}/test", json={})

        assert response.status_code == 401
        assert response.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value


class TestCreateGate:
    """接入前置闸门：不通即 422 且不落库。"""

    async def test_blocked_when_unreachable(self, client: AsyncClient, session, monkeypatch):
        install_probe_transport(monkeypatch, replies(401, _UPSTREAM_ERROR))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "served_model": "gpt-4o",
                "name": "错的 Key",
                "api_key": "sk-bad",
            },
        )

        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value
        assert _UPSTREAM_ERROR in response.json()["message"]
        assert session.all(ModelEndpoint) == []  # 行未被触碰

    async def test_created_when_reachable(self, client: AsyncClient, session, monkeypatch):
        install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "served_model": "gpt-4o",
                "name": "对的 Key",
                "api_key": "sk-live",
            },
        )

        assert response.status_code == 200
        rows = session.all(ModelEndpoint)
        assert len(rows) == 1 and rows[0].name == "对的 Key"

    async def test_connection_failure_also_blocked(self, client: AsyncClient, session, monkeypatch):
        install_probe_transport(monkeypatch, unreachable)

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://nope.invalid/v1",
                "served_model": "gpt-4o",
                "name": "地址不通",
            },
        )

        assert response.status_code == 422
        assert "ConnectError" in response.json()["message"]
        assert session.all(ModelEndpoint) == []

    async def test_skip_probe_bypasses(self, client: AsyncClient, session, monkeypatch):
        seen = install_probe_transport(monkeypatch, unreachable)

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://nope.invalid/v1",
                "served_model": "gpt-4o",
                "name": "放行",
                "skip_probe": True,
            },
        )

        assert response.status_code == 200
        assert seen == []
        assert len(session.all(ModelEndpoint)) == 1

    async def test_global_switch_off_bypasses(self, client: AsyncClient, session, monkeypatch):
        seen = install_probe_transport(monkeypatch, unreachable)
        monkeypatch.setattr(settings, "endpoint_probe_enabled", False)

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://nope.invalid/v1",
                "served_model": "gpt-4o",
                "name": "全局放行",
            },
        )

        assert response.status_code == 200
        assert seen == []


class TestUpdateGate:
    """编辑前置闸门：只有连接配置真的有变才探。"""

    async def test_blocked_when_new_config_unreachable(self, client: AsyncClient, session, monkeypatch):
        row = session.preload(endpoint_row(api_key="sk-stored"))
        install_probe_transport(monkeypatch, replies(401, _UPSTREAM_ERROR))

        response = await client.post(f"{BASE}/7/update", json={"api_key": "sk-typo"})

        assert response.status_code == 422
        assert row.api_key == "sk-stored"  # 未落库，旧配置完好

    async def test_rename_only_skips_probe(self, client: AsyncClient, session, monkeypatch):
        """改名字走同一个 update 接口：连接配置没变就不该插一次上游往返。"""
        session.preload(endpoint_row())
        seen = install_probe_transport(monkeypatch, unreachable)

        response = await client.post(
            f"{BASE}/7/update",
            json={"name": "改名后的端点", "api_base": "https://api.example.com/v1"},
        )

        assert response.status_code == 200
        assert seen == []

    async def test_toggle_enabled_skips_probe(self, client: AsyncClient, session, monkeypatch):
        """前端「启用/停用」开关也走 update：停用一个端点上不该要求上游当场通得过。"""
        session.preload(endpoint_row())
        seen = install_probe_transport(monkeypatch, unreachable)

        response = await client.post(f"{BASE}/7/update", json={"enabled": False})

        assert response.status_code == 200
        assert seen == []

    async def test_disable_with_address_change_skips_probe(self, client: AsyncClient, session, monkeypatch):
        """「停用 + 顺手改地址」是在处理一个已知坏掉的端点：同样不探，停用不被 422 拦死。"""
        session.preload(endpoint_row())
        seen = install_probe_transport(monkeypatch, unreachable)

        response = await client.post(
            f"{BASE}/7/update",
            json={"enabled": False, "api_base": "https://dead.example.com/v1"},
        )

        assert response.status_code == 200
        assert seen == []
        assert session.all(ModelEndpoint)[0].enabled is False

    async def test_omitted_key_falls_back_to_stored(self, client: AsyncClient, session, monkeypatch):
        """编辑弹窗不回传已存 Key 且地址未动：探测须用库里那份，否则改模型名也会 401 被拦。"""
        session.preload(endpoint_row(api_key="sk-stored"))
        seen = install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        response = await client.post(f"{BASE}/7/update", json={"served_model": "gpt-4o-mini"})

        assert response.status_code == 200
        assert seen[0].headers["authorization"] == "Bearer sk-stored"
        assert str(seen[0].url) == "https://api.example.com/v1/chat/completions"
        assert '"model":"gpt-4o-mini"' in seen[0].read().decode()

    async def test_local_endpoint_never_probed(self, client: AsyncClient, session, monkeypatch):
        """本地部署端点改地址不探测：其健康归部署状态投影。"""
        session.preload(endpoint_row(deployment_id="dep-1", engine_type="vllm"))
        seen = install_probe_transport(monkeypatch, unreachable)

        response = await client.post(f"{BASE}/7/update", json={"api_base": "http://127.0.0.1:8000"})

        assert response.status_code == 200
        assert seen == []

    async def test_skip_probe_bypasses(self, client: AsyncClient, session, monkeypatch):
        session.preload(endpoint_row(api_key="sk-stored"))
        seen = install_probe_transport(monkeypatch, unreachable)

        response = await client.post(f"{BASE}/7/update", json={"api_key": "sk-whatever", "skip_probe": True})

        assert response.status_code == 200
        assert seen == []

    async def test_recovers_down_row_when_probe_passes(self, client: AsyncClient, session, monkeypatch):
        """改对 Key 后的修复路径：探通即拨回 healthy，不必等下一次业务调用去自愈。"""
        row = session.preload(endpoint_row(health_status="down", api_key="sk-revoked"))
        install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        response = await client.post(f"{BASE}/7/update", json={"api_key": "sk-fixed"})

        assert response.status_code == 200
        assert row.health_status == "healthy"

    async def test_explicit_health_status_wins(self, client: AsyncClient, session, monkeypatch):
        """显式传了 health_status 就以显式值为准（如「先降级观察」）。"""
        row = session.preload(endpoint_row(health_status="down", api_key="sk-revoked"))
        install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        response = await client.post(f"{BASE}/7/update", json={"api_key": "sk-fixed", "health_status": "degraded"})

        assert response.status_code == 200
        assert row.health_status == "degraded"

    async def test_degraded_row_not_auto_reset(self, client: AsyncClient, session, monkeypatch):
        """``degraded`` 是人工显式标记（探测从不写它）：改配置的编辑不该把它静默放回候选池。"""
        row = session.preload(endpoint_row(health_status="degraded", api_key="sk-revoked"))
        install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        response = await client.post(f"{BASE}/7/update", json={"api_key": "sk-fixed"})

        assert response.status_code == 200
        assert row.health_status == "degraded"

    async def test_skipped_probe_does_not_touch_health(self, client: AsyncClient, session, monkeypatch):
        """跳过探测 = 没验证过，不该顺手套上 healthy。"""
        row = session.preload(endpoint_row(health_status="down", api_key="sk-stored"))
        install_probe_transport(monkeypatch, unreachable)

        response = await client.post(f"{BASE}/7/update", json={"api_key": "sk-rotated", "skip_probe": True})

        assert response.status_code == 200
        assert row.health_status == "down"


class TestSchemaContract:
    """请求体字段契约（前端未改动，新字段必须是可选且向后兼容）。"""

    async def test_extra_fields_ignored(self, client: AsyncClient, session, monkeypatch):
        """老前端不带 skip_probe：请求照常可解（FastAPI 默认忽略未知字段）。"""
        install_probe_transport(monkeypatch, replies(200, _OK_BODY))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "served_model": "gpt-4o",
                "name": "旧前端",
                "unexpected_field": "ignored",
            },
        )

        assert response.status_code == 200


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_protocol_paths(client: AsyncClient, session, monkeypatch, protocol):
    """两种协议各走各的缺省主路径（协议是登记必填字段，形状由探测地址体现）。"""
    seen = install_probe_transport(monkeypatch, replies(200, '{"ok":true}'))

    response = await client.post(
        f"{BASE}/test",
        json={"protocol": protocol, "api_base": "https://api.example.com", "served_model": "m", "api_key": "k"},
    )

    assert response.status_code == 200
    assert str(seen[0].url).endswith("/v1/messages" if protocol == "anthropic" else "/v1/chat/completions")
