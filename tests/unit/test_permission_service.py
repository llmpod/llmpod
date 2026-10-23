"""permission_service.py 单测：权限查询/判定/角色-权限批量装配（MiniDB 真 join 语义）。"""

from __future__ import annotations

import pytest

from llmpod.models.permission import Permission
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.models.user_role import UserRole
from llmpod.services import permission_service as svc
from tests.unit._minidb import MiniDB


@pytest.fixture
def db() -> MiniDB:
    """用户 100 绑定角色 10/11；角色 10 → 权限 1,2；角色 11 → 权限 3；角色 12 无授权。"""
    d = MiniDB()
    d.preload(
        Permission(id=1, scope="platform", resource="cluster", action="read", description="a"),
        Permission(id=2, scope="platform", resource="node", action="read", description="b"),
        Permission(id=3, scope="tenant", resource="model", action="read", description="c"),
    )
    d.preload(
        Role(id=10, tenant_id=0, name="运营", code="operator", is_builtin=True),
        Role(id=11, tenant_id=5, name="租户管理员", code="tenant_admin", is_builtin=True),
        Role(id=12, tenant_id=5, name="无权限角色", code=None, is_builtin=False),
    )
    d.preload(UserRole(user_id=100, role_id=10), UserRole(user_id=100, role_id=11))
    d.preload(
        RolePermission(role_id=10, permission_id=1),
        RolePermission(role_id=10, permission_id=2),
        RolePermission(role_id=11, permission_id=3),
    )
    return d


class TestGetRolePermissions:
    async def test_returns_formatted_codes(self, db):
        assert set(await svc.get_role_permissions(db, 10)) == {
            "platform:cluster:read",
            "platform:node:read",
        }

    async def test_role_without_grants_returns_empty(self, db):
        assert await svc.get_role_permissions(db, 12) == []

    async def test_unknown_role_returns_empty(self, db):
        assert await svc.get_role_permissions(db, 999) == []


class TestGetUserPermissions:
    async def test_union_across_roles(self, db):
        assert await svc.get_user_permissions(db, 100) == {
            "platform:cluster:read",
            "platform:node:read",
            "tenant:model:read",
        }

    async def test_duplicate_grants_deduped(self, db):
        # 两个角色都持有 platform:cluster:read → 并集去重
        db.preload(RolePermission(role_id=11, permission_id=1))
        perms = await svc.get_user_permissions(db, 100)
        assert perms & {"platform:cluster:read"} == {"platform:cluster:read"}
        assert len([p for p in perms if p == "platform:cluster:read"]) == 1

    async def test_user_without_roles_returns_empty(self, db):
        assert await svc.get_user_permissions(db, 200) == set()


class TestUserHasPermission:
    async def test_granted_via_any_role(self, db):
        assert await svc.user_has_permission(db, 100, "tenant", "model", "read") is True

    async def test_shared_by_multiple_roles(self, db):
        """回归：同一权限被多角色同时授予时 join 出多行——不加 limit 1 的话
        scalar_one_or_none 抛 MultipleResultsFound，内层权限校验路由整体 500。"""
        db.preload(RolePermission(role_id=11, permission_id=1))
        assert await svc.user_has_permission(db, 100, "platform", "cluster", "read") is True

    async def test_not_granted_action(self, db):
        # 有 platform:cluster:read 但没有 platform:cluster:create
        assert await svc.user_has_permission(db, 100, "platform", "cluster", "create") is False

    async def test_other_user_not_granted(self, db):
        assert await svc.user_has_permission(db, 200, "platform", "cluster", "read") is False


class TestGetRolePermissionIdsMap:
    async def test_empty_input_short_circuits(self, db):
        assert await svc.get_role_permission_ids_map(db, []) == {}

    async def test_grouped_and_sorted_by_permission_id(self, db):
        # 预置乱序授权（3 在 1 前插入）→ 结果按 permission_id 升序
        d2 = MiniDB()
        d2.preload(
            RolePermission(role_id=10, permission_id=3),
            RolePermission(role_id=10, permission_id=1),
            RolePermission(role_id=11, permission_id=2),
        )
        mapping = await svc.get_role_permission_ids_map(d2, [10, 11])
        assert mapping == {10: [1, 3], 11: [2]}

    async def test_unknown_role_absent_from_mapping(self, db):
        mapping = await svc.get_role_permission_ids_map(db, [12, 10])
        assert mapping == {10: [1, 2]}


class TestEnrichRolesPermissionIds:
    async def test_backfills_in_place_and_returns_same_list(self, db):
        roles = [db._store[Role][0], db._store[Role][1]]
        out = await svc.enrich_roles_permission_ids(db, roles)
        assert out is roles
        assert roles[0].permission_ids == [1, 2]
        assert roles[1].permission_ids == [3]

    async def test_role_without_grants_gets_empty_list(self, db):
        roles = [db._store[Role][2]]  # 角色 12 无授权
        await svc.enrich_roles_permission_ids(db, roles)
        assert roles[0].permission_ids == []

    async def test_empty_list_passthrough(self, db):
        roles: list = []
        assert await svc.enrich_roles_permission_ids(db, roles) is roles
