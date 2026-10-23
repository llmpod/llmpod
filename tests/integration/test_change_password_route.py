"""/auth/change-password 集成测试：自助改密三分支（成功踢会话/旧密错 401/审计落账）。

照项目 HTTP 层测试原则（stub get_db + 鉴权覆盖）：User 行经 FakeSession preload
（db.get 路径），核心逻辑在 auth_service（verify/hash 打桩避免真实 bcrypt），路由内
只验撤销会话与审计。admin 会话 user_id=1 与预置 User 行对齐。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from llmpod.app import app
from llmpod.db import get_db
from llmpod.models.user import User
from tests.integration._fakes import FakeSession


@pytest.fixture
async def session():
    """预置当前用户（id=1，与 admin_session 对齐）的 FakeSession，并接管 get_db。"""
    from llmpod.core.security import hash_password

    s = FakeSession()
    row = User(username="tester", tenant_id=0, password_hash=hash_password("old-secret"))
    row.id = 1
    s.preload(row)

    async def _override():
        yield s

    app.dependency_overrides[get_db] = _override
    yield s
    app.dependency_overrides.clear()


class TestChangePassword:
    async def test_requires_auth(self, client: AsyncClient):
        """无登录态 → 401。"""
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={"old_password": "a", "new_password": "b"},
        )
        assert resp.status_code == 401

    async def test_old_password_mismatch_401(self, admin_client, session, monkeypatch):
        from llmpod.services import auth_service

        audits: list[dict] = []

        async def _audit(**kw):
            audits.append(kw)

        monkeypatch.setattr(auth_service, "verify_password", lambda plain, h: False)
        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.write_audit", _audit)

        resp = await admin_client.post(
            "/api/v1/auth/change-password",
            json={"old_password": "wrong", "new_password": "newpass1"},
        )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "INVALID_CREDENTIALS"
        assert session.all(User)[0].password_hash.startswith("$2b$")  # 未被覆盖
        assert audits and audits[-1]["result"] == "failed"

    async def test_success_updates_hash_and_revokes_token(self, admin_client, session, monkeypatch):
        from llmpod.services import auth_service

        audits: list[dict] = []
        revoked: list[str] = []

        async def _audit(**kw):
            audits.append(kw)

        async def _fake_logout(token, settings):
            revoked.append(token)

        monkeypatch.setattr(auth_service, "verify_password", lambda plain, h: True)
        monkeypatch.setattr(auth_service, "hash_password", lambda p: "$2b$12$newhash")
        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.logout", _fake_logout)
        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.write_audit", _audit)

        resp = await admin_client.post(
            "/api/v1/auth/change-password",
            json={"old_password": "oldpass", "new_password": "newpass1"},
            headers={"Authorization": "Bearer the-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == {"changed": True}
        assert session.all(User)[0].password_hash == "$2b$12$newhash"
        assert revoked == ["the-token"]  # 当前会话被撤销
        assert audits and audits[-1]["result"] == "success"
        assert audits[-1]["resource_id"] == "1"  # 审计指向自己
