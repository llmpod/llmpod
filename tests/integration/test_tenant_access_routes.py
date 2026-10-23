"""租户端角色与授权管理路由集成测试（/api/v1/tenant/access/*，stub 驱动）。

覆盖 routes/tenant/access.py 的读写主体（与 tests/integration/
test_admin_access_routes.py 对称，但强制租户作用域 tenant_id=5）：
- 权限选择器固定 scope=tenant；
- 角色 CRUD（404 作用域守卫 / 内置禁删 / 级联清理）；
- 角色-权限全量替换（去重）与单项回收；
- 用户-角色幂等授权（跨租户角色 409）、回收（最后 tenant_admin 守卫 400）。

桩惯例同 admin 侧：OpSession 接管 get_db；tenant_session 短路权限门并经
current_tenant_id 提供租户边界；join 型 service monkeypatch 路由模块命名空间；
审计落库 no-op。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from llmpod.app import app
from llmpod.db import get_db
from llmpod.models.permission import Permission
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.models.user import User
from llmpod.models.user_role import UserRole

from _fakes import tenant_session
from _op_session import OpSession

TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
TENANT_ID = 5


def _role(rid: int, tenant_id: int, code: str | None = None, builtin: bool = False) -> Role:
    return Role(
        id=rid, tenant_id=tenant_id, name=f"role-{rid}", code=code, is_builtin=builtin, created_at=TS, updated_at=TS
    )


def _base_session() -> OpSession:
    s = OpSession()
    s.preload(Permission(id=301, scope="tenant", resource="user", action="read"))
    s.preload(Permission(id=302, scope="tenant", resource="user", action="create"))
    s.preload(Permission(id=101, scope="platform", resource="cluster", action="read"))
    # 本租户角色：21 自定义（已授权 301）、22 内置 tenant_admin；23 平台侧（越界样本）
    s.preload(_role(21, TENANT_ID))
    s.preload(_role(22, TENANT_ID, code="tenant_admin", builtin=True))
    s.preload(_role(23, tenant_id=0))
    s.preload(RolePermission(role_id=21, permission_id=301))
    # 本租户用户 9/10；平台用户 1（越界样本）
    s.preload(User(id=9, tenant_id=TENANT_ID, username="owner", password_hash="x", created_at=TS, updated_at=TS))
    s.preload(User(id=10, tenant_id=TENANT_ID, username="member", password_hash="x", created_at=TS, updated_at=TS))
    s.preload(User(id=1, tenant_id=0, username="platform", password_hash="x", created_at=TS, updated_at=TS))
    # 租户超管 user 9 持 tenant_admin（最后管理员守卫用例）
    s.preload(UserRole(user_id=9, role_id=22))
    return s


@pytest.fixture
async def session():
    s = _base_session()

    async def _override():
        yield s

    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[get_current_user] = lambda: tenant_session(tenant_id=TENANT_ID, user_id=9)
    yield s
    app.dependency_overrides.clear()


def _flush_boom(s: OpSession):
    async def _raise():
        raise IntegrityError("INSERT ...", {}, Exception("uq_role_tenant_code"))

    s.flush = _raise  # type: ignore[method-assign]


async def _no_audit(_entry):
    return None


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)


from llmpod.apps.webserver.auth.deps import get_current_user  # noqa: E402 — fixture 定义后才 import，保持就近

BASE = "/api/v1/tenant/access"


class TestPermissionSelector:
    async def test_lists_tenant_scope_only(self, client: AsyncClient, session):
        resp = await client.get(f"{BASE}/permissions")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert {p["scope"] for p in data} == {"tenant"}  # 平台域权限不外泄
        assert [(p["resource"], p["action"]) for p in data] == [("user", "create"), ("user", "read")]


class TestRoleCrud:
    async def test_list_roles_scoped_and_enriched(self, client: AsyncClient, session):
        resp = await client.get(f"{BASE}/roles")
        assert resp.status_code == 200
        by_id = {r["id"]: r for r in resp.json()["data"]}
        assert set(by_id) == {21, 22}  # 平台侧角色 23 不进租户视图
        assert by_id[21]["permission_ids"] == [301]

    async def test_create_role_binds_current_tenant(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles", json={"name": "auditor"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["tenant_id"] == TENANT_ID
        assert data["is_builtin"] is False
        assert data["permission_ids"] == []

    async def test_create_role_conflict(self, client: AsyncClient, session):
        _flush_boom(session)
        resp = await client.post(f"{BASE}/roles", json={"name": "dup"})
        assert resp.status_code == 409

    async def test_update_role(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles/21/update", json={"name": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "renamed"

    async def test_update_role_empty_body_422(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles/21/update", json={})
        assert resp.status_code == 422

    async def test_update_platform_role_404(self, client: AsyncClient, session):
        """租户端改平台域角色 → 作用域守卫 404。"""
        resp = await client.post(f"{BASE}/roles/23/update", json={"name": "x"})
        assert resp.status_code == 404

    async def test_delete_role_cascades(self, client: AsyncClient, session):
        session.preload(UserRole(user_id=10, role_id=21))
        resp = await client.post(f"{BASE}/roles/21/delete")
        assert resp.status_code == 200
        assert await session.get(Role, 21) is None
        assert not [rp for rp in session.all(RolePermission) if rp.role_id == 21]
        assert not [ur for ur in session.all(UserRole) if ur.role_id == 21]

    async def test_delete_builtin_role_409(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles/22/delete")
        assert resp.status_code == 409

    async def test_delete_cross_tenant_role_404(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles/23/delete")
        assert resp.status_code == 404


class TestRolePermissions:
    async def test_list_role_permissions(self, client: AsyncClient, session, monkeypatch):
        async def _perms(db, role_id):
            return ["tenant:user:read"]

        monkeypatch.setattr("llmpod.apps.webserver.routes.tenant.access.get_role_permissions", _perms)
        resp = await client.get(f"{BASE}/roles/21/permissions")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["tenant:user:read"]

    async def test_replace_role_permissions_dedups(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles/21/permissions", json={"permission_ids": [302, 301, 301]})
        assert resp.status_code == 200
        assert resp.json()["data"] == {"role_id": 21, "permission_ids": [301, 302]}
        assert sorted(rp.permission_id for rp in session.all(RolePermission) if rp.role_id == 21) == [301, 302]

    async def test_replace_role_permissions_cross_tenant_404(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles/23/permissions", json={"permission_ids": [301]})
        assert resp.status_code == 404

    async def test_replace_role_permissions_invalid_id_409(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles/21/permissions", json={"permission_ids": [301, 999]})
        assert resp.status_code == 409
        # 无效 ID 全量拒绝：不清空旧关联（301 仍在）
        assert any(rp.permission_id == 301 for rp in session.all(RolePermission) if rp.role_id == 21)

    async def test_replace_role_permissions_cross_scope_409(self, client: AsyncClient, session):
        # 101 是平台域权限，不得静默落进租户域角色
        resp = await client.post(f"{BASE}/roles/21/permissions", json={"permission_ids": [301, 101]})
        assert resp.status_code == 409

    async def test_revoke_role_permission(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/roles/21/permissions/301/revoke")
        assert resp.status_code == 200
        assert resp.json()["data"]["revoked"] is True
        assert not [rp for rp in session.all(RolePermission) if rp.role_id == 21]


class TestUserRoles:
    async def test_list_user_permissions(self, client: AsyncClient, session, monkeypatch):
        async def _perms(db, user_id):
            return {"tenant:user:read", "tenant:user:create"}

        monkeypatch.setattr("llmpod.apps.webserver.routes.tenant.access.get_user_permissions", _perms)
        resp = await client.get(f"{BASE}/users/9/permissions")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["tenant:user:create", "tenant:user:read"]

    async def test_list_user_permissions_cross_tenant_404(self, client: AsyncClient, session):
        """平台域用户在租户端不可见（作用域守卫）。"""
        resp = await client.get(f"{BASE}/users/1/permissions")
        assert resp.status_code == 404

    async def test_assign_user_roles_dedup(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/users/10/roles", json={"role_ids": [21, 21]})
        assert resp.status_code == 200
        assert resp.json()["data"] == {"user_id": 10, "added": [21]}
        resp = await client.post(f"{BASE}/users/10/roles", json={"role_ids": [21]})
        assert resp.json()["data"]["added"] == []

    async def test_assign_user_roles_rejects_foreign_role(self, client: AsyncClient, session):
        """本租户用户绑平台域角色 → 409。"""
        resp = await client.post(f"{BASE}/users/10/roles", json={"role_ids": [23]})
        assert resp.status_code == 409
        assert "非本租户" in resp.json()["message"]

    async def test_assign_user_roles_missing_user_404(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/users/999/roles", json={"role_ids": [21]})
        assert resp.status_code == 404

    async def test_revoke_user_role_keeps_last_tenant_admin(self, client: AsyncClient, session):
        """user 9 是 tenant_admin 最后一名持有者 → 回收被拒（400），租户不"无主"。"""
        resp = await client.post(f"{BASE}/users/9/roles/22/revoke")
        assert resp.status_code == 400
        assert "至少需保留一个管理员" in resp.json()["message"]
        assert await session.get(UserRole, (9, 22)) is not None

    async def test_revoke_user_role_with_other_holder(self, client: AsyncClient, session):
        session.preload(UserRole(user_id=10, role_id=22))
        resp = await client.post(f"{BASE}/users/9/roles/22/revoke")
        assert resp.status_code == 200
        assert await session.get(UserRole, (9, 22)) is None
        assert await session.get(UserRole, (10, 22)) is not None

    async def test_revoke_user_role_not_held_is_noop(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/users/10/roles/22/revoke")
        assert resp.status_code == 200
        assert await session.get(UserRole, (9, 22)) is not None

    async def test_revoke_user_role_missing_role_404(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/users/9/roles/999/revoke")
        assert resp.status_code == 404
