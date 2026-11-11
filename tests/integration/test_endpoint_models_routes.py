"""来源端点登记与模型清单的路由级集成测试（stub get_db + httpx.MockTransport，不连真网络/PG）。

覆盖 ``POST /admin/endpoints``（统一登记：模型数组，单条即长度 1）与
``POST /admin/endpoints/models``（拉清单）两条路由的契约：

- 登记：验证分两层——第一个模型 chat 强验证（真生成，验 Key/地址/path），其余
  模型清单成员资格（一次 GET /models）；N=1 只剩 chat 层。全过才写（请求内重名 /
  库内已登记 / 探测不通 / 其余模型不在清单 → 422 点名且一行不落）；跳过校验
  （全局开关 / skip_probe）时零上游调用；name 缺省回 served_model；
- 拉清单：200 带去重排序 id；上游拒绝 422 带原文（区别于 /test 的恒 200——
  本接口的产物就是清单，失败即无法履约）；
- 两条路由均受 ``require_permission`` 保护（无会话 401）。
"""

from __future__ import annotations


import pytest
import pytest_asyncio
from httpx import AsyncClient

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.config.settings import settings
from llmpod.db import get_db
from llmpod.models import ModelEndpoint

from _endpoint_probe_helpers import endpoint_row, install_probe_transport, models_payload, replies
from _fakes import admin_session
from _op_session import OpSession

BASE = "/api/v1/admin/endpoints"


@pytest_asyncio.fixture
async def session():
    """桩会话 + 超管会话（权限短路，不触库）；审计写库打桩。"""

    async def _no_audit(_entry):
        return None

    mp = pytest.MonkeyPatch()
    mp.setattr("llmpod.services.audit_service._write_audit", _no_audit)
    s = OpSession()

    async def _override_db():
        yield s

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: admin_session()
    yield s
    app.dependency_overrides.clear()
    mp.undo()


class TestUnauthorized:
    """无会话 → 401：证明路由已注册且受 require_permission 保护。"""

    async def test_create_requires_auth(self, client: AsyncClient):
        response = await client.post(
            BASE,
            json={"protocol": "openai", "api_base": "https://api.example.com/v1", "models": [{"served_model": "m1"}]},
        )
        assert response.status_code == 401

    async def test_models_requires_auth(self, client: AsyncClient):
        response = await client.post(
            f"{BASE}/models", json={"protocol": "openai", "api_base": "https://api.example.com/v1"}
        )
        assert response.status_code == 401


class TestFetchModelsRoute:
    """``POST /admin/endpoints/models``：发现出口，产物就是清单。"""

    async def test_returns_sorted_models(self, client: AsyncClient, session, monkeypatch):
        seen = install_probe_transport(monkeypatch, replies(200, models_payload("b-model", "a-model", "b-model")))

        response = await client.post(
            f"{BASE}/models",
            json={"protocol": "openai", "api_base": "https://api.example.com/v1", "api_key": "sk-1"},
        )

        assert response.status_code == 200
        assert response.json()["data"] == ["a-model", "b-model"]
        assert seen[0].url == "https://api.example.com/v1/models"
        assert seen[0].headers["authorization"] == "Bearer sk-1"

    async def test_upstream_rejection_is_422_with_verbatim_detail(self, client: AsyncClient, session, monkeypatch):
        """上游拒绝 = 本次无法履约：422 带上游原文（与 /test 的恒 200 报告契约不同）。"""
        upstream_error = '{"error":{"message":"Incorrect API key"}}'
        install_probe_transport(monkeypatch, replies(401, upstream_error))

        response = await client.post(
            f"{BASE}/models",
            json={"protocol": "openai", "api_base": "https://api.example.com/v1"},
        )

        assert response.status_code == 422
        message = response.json()["message"]
        assert "上游模型清单拉取失败" in message
        assert upstream_error in message

    async def test_invalid_protocol_rejected(self, client: AsyncClient, session):
        response = await client.post(
            f"{BASE}/models", json={"protocol": "grpc", "api_base": "https://api.example.com/v1"}
        )
        assert response.status_code == 422


class TestCreateRoute:
    """``POST /admin/endpoints``：统一登记（模型数组），全过才写，一行不落。"""

    async def test_single_model_chats_only(self, client: AsyncClient, session, monkeypatch):
        """单条（长度 1）：只发一次 chat 强验证，不拉清单——与旧单条登记行为一致。"""
        seen = install_probe_transport(monkeypatch, replies(200))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "api_key": "sk-1",
                "models": [{"served_model": "m1", "name": "展示名"}],
            },
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert [row["served_model"] for row in data] == ["m1"]
        assert data[0]["name"] == "展示名"
        assert data[0]["upstream_type"] == "external"
        # 只有一次上游调用：chat 探测（POST 主路径），没有 GET /models
        assert len(seen) == 1
        assert seen[0].method == "POST"
        assert seen[0].url == "https://api.example.com/v1/chat/completions"
        rows = session.all(ModelEndpoint)
        assert len(rows) == 1 and rows[0].api_key == "sk-1" and rows[0].enabled

    async def test_creates_all_models(self, client: AsyncClient, session, monkeypatch):
        """多模型：第一个 chat 强验证 + 一次 GET /models 成员资格，建 N 行。"""
        seen = install_probe_transport(monkeypatch, replies(200, models_payload("m1", "m2")))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "api_key": "sk-1",
                "models": [{"served_model": "m1"}, {"served_model": "m2", "name": "展示名"}],
            },
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert [row["served_model"] for row in data] == ["m1", "m2"]  # 请求序
        assert data[0]["name"] == "m1"  # 缺省回模型名
        assert data[1]["name"] == "展示名"
        # 两次上游调用：chat 探第一个（POST 主路径）+ 一次 GET /models（非逐模型 chat）
        assert [r.method for r in seen] == ["POST", "GET"]
        assert seen[0].url == "https://api.example.com/v1/chat/completions"
        assert seen[1].url == "https://api.example.com/v1/models"
        rows = session.all(ModelEndpoint)
        assert sorted(r.served_model for r in rows) == ["m1", "m2"]
        assert all(r.api_key == "sk-1" and r.enabled for r in rows)

    async def test_first_model_probe_failure_rejects_without_writing(self, client: AsyncClient, session, monkeypatch):
        """chat 强验证不通（Key 错/地址错）：422 带上游原文，一行不落。"""
        upstream_error = '{"error":{"message":"Incorrect API key"}}'
        seen = install_probe_transport(monkeypatch, replies(401, upstream_error))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "models": [{"served_model": "m1"}],
            },
        )

        assert response.status_code == 422
        assert upstream_error in response.json()["message"]
        assert session.all(ModelEndpoint) == []
        assert len(seen) == 1  # chat 失败即止，不再拉清单

    async def test_missing_model_rejected_without_writing(self, client: AsyncClient, session, monkeypatch):
        """其余模型不在上游清单：422 点名缺失项，一行不落（chat 只覆盖第一个）。"""
        install_probe_transport(monkeypatch, replies(200, models_payload("m1")))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "models": [{"served_model": "m1"}, {"served_model": "m-gone"}],
            },
        )

        assert response.status_code == 422
        assert "m-gone" in response.json()["message"]
        assert session.all(ModelEndpoint) == []

    async def test_existing_registration_rejected(self, client: AsyncClient, session, monkeypatch):
        """库内已有同 (api_base, protocol, served_model) 外部端点：422 点名，不重复建。"""
        session.preload(endpoint_row(served_model="m1"))
        install_probe_transport(monkeypatch, replies(200, models_payload("m1", "m2")))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "models": [{"served_model": "m1"}, {"served_model": "m2"}],
            },
        )

        assert response.status_code == 422
        assert "m1" in response.json()["message"]
        assert len(session.all(ModelEndpoint)) == 1  # 只剩预置那行

    async def test_existing_other_base_not_rejected(self, client: AsyncClient, session, monkeypatch):
        """不同 api_base 的同名模型不算重复（同一模型多供应商接入是正当需求）。"""
        session.preload(endpoint_row(api_base="https://other.example.com/v1", served_model="m1"))
        install_probe_transport(monkeypatch, replies(200))

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "models": [{"served_model": "m1"}],
            },
        )

        assert response.status_code == 200
        assert len(session.all(ModelEndpoint)) == 2

    async def test_in_request_duplicates_rejected(self, client: AsyncClient, session):
        """请求内重名：纯本地校验，不触上游。"""
        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "models": [{"served_model": "m1"}, {"served_model": "m1"}],
            },
        )

        assert response.status_code == 422
        assert "m1" in response.json()["message"]
        assert session.all(ModelEndpoint) == []

    async def test_skip_probe_skips_upstream_call(self, client: AsyncClient, session, monkeypatch):
        """skip_probe=true：chat 与清单两层全跳，不连上游直接登记（探到桩即红）。"""
        seen = install_probe_transport(
            monkeypatch,
            lambda request: pytest.fail("skip_probe=true 不应连上游"),
        )

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "models": [{"served_model": "m1"}, {"served_model": "m2"}],
                "skip_probe": True,
            },
        )

        assert response.status_code == 200
        assert seen == []
        assert len(session.all(ModelEndpoint)) == 2

    async def test_global_switch_disables_verification(self, client: AsyncClient, session, monkeypatch):
        """全局 endpoint_probe_enabled=False（无外网出口兜底）：跳过校验对齐。"""
        monkeypatch.setattr(settings, "endpoint_probe_enabled", False)
        install_probe_transport(
            monkeypatch,
            lambda request: pytest.fail("全局探测开关关闭时不应连上游"),
        )

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "models": [{"served_model": "m1"}],
            },
        )

        assert response.status_code == 200
        assert len(session.all(ModelEndpoint)) == 1

    async def test_body_validation(self, client: AsyncClient, session):
        """空 models / 超上限 → 422（pydantic 契约，不触上游）。"""
        response = await client.post(
            BASE, json={"protocol": "openai", "api_base": "https://api.example.com/v1", "models": []}
        )
        assert response.status_code == 422

        response = await client.post(
            BASE,
            json={
                "protocol": "openai",
                "api_base": "https://api.example.com/v1",
                "models": [{"served_model": f"m{i}"} for i in range(101)],
            },
        )
        assert response.status_code == 422
        assert session.all(ModelEndpoint) == []
