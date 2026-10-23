"""平台权限管理路由集成测试（/api/v1/admin/access/*，stub 驱动）。

覆盖 routes/admin/access.py 的读写主体：
- 权限选择器按 scope 过滤；
- 角色 CRUD（创建冲突 409 / 更新空体 422 / 跨租户 404 / 内置角色禁删 409 / 级联清理）；
- 角色-权限全量替换（去重）与单项回收；
- 用户-角色幂等授权（跨租户角色 409）、回收（最后管理员守卫 400 / 幂等 no-op）。

桩惯例：OpSession（tests/integration/_op_session.py，运算符语义过滤 + 单列投影
1 元组）接管 get_db；超管会话短路外层门禁/内层权限校验；join 型 service
（get_role_permissions / get_user_permissions）按项目惯例 monkeypatch 路由模块
命名空间符号；审计落库打 no-op（不触真实 PG）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from llmpod.app import app
from llmpod.config.constants import PLATFORM_TENANT_ID
from llmpod.db import get_db
from llmpod.models.permission import Permission
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.models.user import User
from llmpod.models.user_role import UserRole

from _fakes import admin_session
from _op_session import OpSession

TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _role(rid: int, tenant_id: int = 0, code: str | None = None, builtin: bool = False) -> Role:
    return Role(
        id=rid, tenant_id=tenant_id, name=f"role-{rid}", code=code, is_builtin=builtin, created_at=TS, updated_at=TS
    )


def _base_session() -> OpSession:
    """预置平台域角色/权限/用户 + 越界样本（租户侧角色/用户）的会话。"""
    s = OpSession()
    # 权限：平台两条 + 租户一条（scope 过滤用）
    s.preload(Permission(id=101, scope="platform", resource="cluster", action="read"))
    s.preload(Permission(id=102, scope="platform", resource="cluster", action="create"))
    s.preload(Permission(id=201, scope="tenant", resource="user", action="read"))
    # 角色：11 平台自定义（已授权 101）、12 平台内置 super_admin、13 租户侧（越界样本）
    s.preload(_role(11))
    s.preload(_role(12, code="super_admin", builtin=True))
    s.preload(_role(13, tenant_id=5))
    s.preload(RolePermission(role_id=11, permission_id=101))
    # 用户：1/2 平台域、3 租户域（越界样本）
    s.preload(User(id=1, tenant_id=0, username="admin", password_hash="x", created_at=TS, updated_at=TS))
    s.preload(User(id=2, tenant_id=0, username="alice", password_hash="x", created_at=TS, updated_at=TS))
    s.preload(User(id=3, tenant_id=5, username="tenant-u", password_hash="x", created_at=TS, updated_at=TS))
    # 会话用户持 super_admin（内置，最后管理员守卫用例用）
    s.preload(UserRole(user_id=1, role_id=12))
    return s


@pytest.fixture
async def session():
    s = _base_session()

    async def _override():
        yield s

    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[get_current_user] = lambda: admin_session()
    yield s
    app.dependency_overrides.clear()


def _flush_boom(s: OpSession):
    """把会话 flush 换成 IntegrityError（模拟唯一约束冲突 → 路由 409 分支）。"""

    async def _raise():
        raise IntegrityError("INSERT ...", {}, Exception("uq_role_tenant_code"))

    s.flush = _raise  # type: ignore[method-assign]


async def _no_audit(_entry):
    return None


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)


from llmpod.apps.webserver.auth.deps import get_current_user  # noqa: E402 — fixture 定义后才 import，保持就近


class TestPermissionSelector:
    async def test_default_lists_platform_only_sorted(self, client: AsyncClient, session):
        resp = await client.get("/api/v1/admin/access/permissions")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert {p["scope"] for p in data} == {"platform"}  # 不泄露租户侧权限
        # order_by(resource, action)：cluster:read 在 cluster:create 之后
        assert [(p["resource"], p["action"]) for p in data] == [("cluster", "create"), ("cluster", "read")]

    async def test_explicit_tenant_scope(self, client: AsyncClient, session):
        resp = await client.get("/api/v1/admin/access/permissions?scope=tenant")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert {p["scope"] for p in data} == {"tenant"}


class TestRoleAuditAttribution:
    """平台域角色管理的审计行归属 = 平台租户 0（资源自身带租户，不再落 NULL）。

    本文件 autouse fixture 会静音审计，故此处重新接管 ``_write_audit`` 抓行。
    """

    async def test_create_role_records_platform_tenant(self, client: AsyncClient, session, monkeypatch):
        entries = []

        async def _capture(entry):
            entries.append(entry)

        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _capture)

        resp = await client.post("/api/v1/admin/access/roles", json={"name": "auditor"})
        assert resp.status_code == 200
        assert len(entries) == 1
        assert entries[0].resource_type == "access"
        assert entries[0].tenant_id == PLATFORM_TENANT_ID


class TestRoleCrud:
    async def test_list_roles_enriches_permission_ids(self, client: AsyncClient, session):
        resp = await client.get("/api/v1/admin/access/roles")
        assert resp.status_code == 200
        data = resp.json()["data"]
        by_id = {r["id"]: r for r in data}
        assert set(by_id) == {11, 12}  # 租户侧角色 13 不出现在管理端
        assert by_id[11]["permission_ids"] == [101]
        assert by_id[12]["permission_ids"] == []

    async def test_create_role(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/roles", json={"name": "viewer"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["name"] == "viewer"
        assert data["tenant_id"] == 0
        assert data["is_builtin"] is False
        assert data["permission_ids"] == []
        assert any(r.id == data["id"] for r in session.all(Role))

    async def test_create_role_conflict(self, client: AsyncClient, session):
        _flush_boom(session)
        resp = await client.post("/api/v1/admin/access/roles", json={"name": "dup"})
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "CONFLICT"

    async def test_update_role(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/roles/11/update", json={"name": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "renamed"

    async def test_update_role_empty_body_422(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/roles/11/update", json={})
        assert resp.status_code == 422

    async def test_update_cross_tenant_role_404(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/roles/13/update", json={"name": "x"})
        assert resp.status_code == 404

    async def test_delete_role_cascades_grants(self, client: AsyncClient, session):
        session.preload(UserRole(user_id=2, role_id=11))
        resp = await client.post("/api/v1/admin/access/roles/11/delete")
        assert resp.status_code == 200
        assert resp.json()["data"] == {"id": 11, "deleted": True}
        assert await session.get(Role, 11) is None
        # 级联清理授权/持有关联（逻辑外键）
        assert not [rp for rp in session.all(RolePermission) if rp.role_id == 11]
        assert not [ur for ur in session.all(UserRole) if ur.role_id == 11]

    async def test_delete_builtin_role_409(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/roles/12/delete")
        assert resp.status_code == 409

    async def test_delete_missing_role_404(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/roles/999/delete")
        assert resp.status_code == 404


class TestRolePermissions:
    async def test_list_role_permissions(self, client: AsyncClient, session, monkeypatch):
        async def _perms(db, role_id):
            return ["platform:cluster:read"]

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.access.get_role_permissions", _perms)
        resp = await client.get("/api/v1/admin/access/roles/11/permissions")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["platform:cluster:read"]

    async def test_list_role_permissions_cross_tenant_404(self, client: AsyncClient, session):
        resp = await client.get("/api/v1/admin/access/roles/13/permissions")
        assert resp.status_code == 404

    async def test_replace_role_permissions_dedups(self, client: AsyncClient, session):
        resp = await client.post(
            "/api/v1/admin/access/roles/11/permissions",
            json={"permission_ids": [102, 101, 102]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == {"role_id": 11, "permission_ids": [101, 102]}
        # 旧关联清空后按去重集合写入
        assert sorted(rp.permission_id for rp in session.all(RolePermission) if rp.role_id == 11) == [101, 102]

    async def test_replace_role_permissions_missing_role_404(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/roles/13/permissions", json={"permission_ids": [101]})
        assert resp.status_code == 404

    async def test_replace_role_permissions_invalid_id_409(self, client: AsyncClient, session):
        resp = await client.post(
            "/api/v1/admin/access/roles/11/permissions",
            json={"permission_ids": [101, 999]},
        )
        assert resp.status_code == 409
        # 无效 ID 全量拒绝：不清空旧关联（101 仍在）
        assert any(rp.permission_id == 101 for rp in session.all(RolePermission) if rp.role_id == 11)

    async def test_replace_role_permissions_cross_scope_409(self, client: AsyncClient, session):
        # 201 是租户域权限，不得静默落进平台域角色
        resp = await client.post(
            "/api/v1/admin/access/roles/11/permissions",
            json={"permission_ids": [101, 201]},
        )
        assert resp.status_code == 409

    async def test_revoke_role_permission(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/roles/11/permissions/101/revoke")
        assert resp.status_code == 200
        assert resp.json()["data"] == {"role_id": 11, "permission_id": 101, "revoked": True}
        assert not [rp for rp in session.all(RolePermission) if rp.role_id == 11]


class TestUserRoles:
    async def test_list_user_permissions(self, client: AsyncClient, session, monkeypatch):
        async def _perms(db, user_id):
            return {"platform:cluster:read", "platform:cluster:create"}

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.access.get_user_permissions", _perms)
        resp = await client.get("/api/v1/admin/access/users/1/permissions")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["platform:cluster:create", "platform:cluster:read"]  # sorted

    async def test_list_user_permissions_cross_tenant_404(self, client: AsyncClient, session):
        resp = await client.get("/api/v1/admin/access/users/3/permissions")
        assert resp.status_code == 404

    async def test_assign_user_roles_dedup_and_idempotent(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/users/2/roles", json={"role_ids": [11, 11]})
        assert resp.status_code == 200
        assert resp.json()["data"] == {"user_id": 2, "added": [11]}
        # 幂等：已持有则不再 added
        resp = await client.post("/api/v1/admin/access/users/2/roles", json={"role_ids": [11]})
        assert resp.status_code == 200
        assert resp.json()["data"]["added"] == []

    async def test_assign_user_roles_rejects_cross_tenant_role(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/users/2/roles", json={"role_ids": [13]})
        assert resp.status_code == 409
        assert "非平台域" in resp.json()["message"]

    async def test_assign_user_roles_missing_user_404(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/users/999/roles", json={"role_ids": [11]})
        assert resp.status_code == 404

    async def test_revoke_user_role_keeps_last_admin(self, client: AsyncClient, session):
        """user 1 是 super_admin 最后一名持有者 → 回收被拒（400）。"""
        resp = await client.post("/api/v1/admin/access/users/1/roles/12/revoke")
        assert resp.status_code == 400
        assert "至少需保留一个管理员" in resp.json()["message"]
        assert await session.get(UserRole, (1, 12)) is not None  # 未被回收

    async def test_revoke_user_role_with_other_holder(self, client: AsyncClient, session):
        session.preload(UserRole(user_id=2, role_id=12))
        resp = await client.post("/api/v1/admin/access/users/1/roles/12/revoke")
        assert resp.status_code == 200
        assert await session.get(UserRole, (1, 12)) is None
        assert await session.get(UserRole, (2, 12)) is not None

    async def test_revoke_user_role_not_held_is_noop(self, client: AsyncClient, session):
        """未持有该角色的用户回收 → 幂等 no-op，不触发最后管理员校验。"""
        resp = await client.post("/api/v1/admin/access/users/2/roles/12/revoke")
        assert resp.status_code == 200
        assert await session.get(UserRole, (1, 12)) is not None

    async def test_revoke_user_role_missing_role_404(self, client: AsyncClient, session):
        resp = await client.post("/api/v1/admin/access/users/1/roles/999/revoke")
        assert resp.status_code == 404
