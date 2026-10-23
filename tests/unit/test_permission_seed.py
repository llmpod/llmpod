"""permission/seed.py 单测：RBAC 种子幂等 upsert / 孤儿清理 / 内置角色授权 / 一致性检查。

用 ``_minidb.MiniDB``（按语句 AST 真过滤的内存 session）承载权限/角色/授权行，
全离线。注册表是进程级全局：沿用 test_permission_registry 的
「清空 → reload platform/tenant → register_all」范式保证隔离。
"""

from __future__ import annotations

import importlib
import logging

import pytest

import llmpod.permission.platform
import llmpod.permission.tenant
from llmpod.config.constants import PLATFORM_TENANT_ID
from llmpod.models.permission import Permission
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.permission import seed as seed_mod
from llmpod.permission.menu import MenuSpec
from llmpod.permission.permissions import BUILTIN_ROLE_PERMISSIONS
from llmpod.permission.registry import PermSpec, _reset_for_test, registered_permissions
from tests.unit._minidb import MiniDB


@pytest.fixture(autouse=True)
def _clean_registry():
    """每用例前后重置权限注册表并重建声明（与 test_permission_registry 同范式）。"""
    _reset_for_test()
    importlib.reload(llmpod.permission.platform)
    importlib.reload(llmpod.permission.tenant)
    from llmpod.permission.permissions import register_all

    register_all()
    yield
    _reset_for_test()
    importlib.reload(llmpod.permission.platform)
    importlib.reload(llmpod.permission.tenant)
    from llmpod.permission.permissions import register_all

    register_all()


def _perm_count() -> int:
    return len(registered_permissions())


def _perm(spec: PermSpec, pid: int, description: str | None = None) -> Permission:
    return Permission(
        id=pid,
        scope=spec.scope,
        resource=spec.resource,
        action=spec.action,
        description=spec.description if description is None else description,
    )


def _rp_of(db: MiniDB, role: Role) -> set[int]:
    return {rp.permission_id for rp in db.rows_of(RolePermission) if rp.role_id == role.id}


class TestSeedPermissionsUpsert:
    async def test_creates_all_registered_permissions_with_description(self):
        db = MiniDB()
        await seed_mod.seed_permissions(db)
        rows = db.rows_of(Permission)
        assert len(rows) == _perm_count()
        keys = {(p.scope, p.resource, p.action) for p in rows}
        assert keys == {(s.scope, s.resource, s.action) for s in registered_permissions()}
        # 全部带 description（声明处非空 → 落库非空）
        assert all(p.description for p in rows)

    async def test_backfills_missing_description_on_existing_row(self):
        spec = registered_permissions()[0]
        db = MiniDB()
        stale = _perm(spec, pid=1, description=None)
        db.preload(stale)
        await seed_mod.seed_permissions(db)
        # 已存在但缺 description → 补全而非重建（同 id 行原地补）
        assert len(db.rows_of(Permission)) == _perm_count()
        assert stale.description == spec.description

    async def test_existing_description_not_overwritten(self):
        spec = registered_permissions()[0]
        db = MiniDB()
        existing = _perm(spec, pid=1, description="旧描述")
        db.preload(existing)
        await seed_mod.seed_permissions(db)
        assert existing.description == "旧描述"

    async def test_idempotent_second_run_no_dup(self):
        db = MiniDB()
        await seed_mod.seed_permissions(db)
        first = len(db.rows_of(Permission))
        await seed_mod.seed_permissions(db)
        assert len(db.rows_of(Permission)) == first


class TestSeedPermissionsOrphanCleanup:
    async def test_unreferenced_unregistered_orphan_removed(self):
        db = MiniDB()
        stale = Permission(id=99, scope="platform", resource="legacy", action="purge", description="历史遗留")
        db.preload(stale)
        await seed_mod.seed_permissions(db)
        keys = {(p.scope, p.resource, p.action) for p in db.rows_of(Permission)}
        assert ("platform", "legacy", "purge") not in keys
        assert len(db.rows_of(Permission)) == _perm_count()

    async def test_unregistered_but_referenced_permission_kept(self):
        """已废弃但仍有角色授权引用 → 不删（防脏 FK）。"""
        db = MiniDB()
        stale = Permission(id=99, scope="platform", resource="legacy", action="purge", description="x")
        role = Role(id=10, tenant_id=PLATFORM_TENANT_ID, name="op", code="operator", is_builtin=True)
        db.preload(stale, role)
        db.preload(RolePermission(role_id=10, permission_id=99))
        await seed_mod.seed_permissions(db)
        assert any(p.id == 99 for p in db.rows_of(Permission))

    async def test_empty_registry_skips_cleanup_with_warning(self, caplog):
        """空注册表（异常态）→ 跳过孤儿清理并告警，不清空整表。"""
        _reset_for_test()  # 清空注册表（fixture 已 register_all，这里模拟空注册）
        db = MiniDB()
        keep = Permission(id=1, scope="platform", resource="cluster", action="read", description="x")
        db.preload(keep)
        with caplog.at_level(logging.WARNING, logger="llmpod.permission.seed"):
            await seed_mod.seed_permissions(db)
        assert any("无已登记权限" in r.message for r in caplog.records)
        assert [p.id for p in db.rows_of(Permission)] == [1]  # 未误删


class TestSeedPermissionsRoleGrants:
    async def test_grants_platform_builtin_operator_role(self):
        """tenant_id=0 的 operator 角色按 BUILTIN_ROLE_PERMISSIONS 全量授权。"""
        db = MiniDB()
        operator = Role(id=10, tenant_id=PLATFORM_TENANT_ID, name="运营", code="operator", is_builtin=True)
        db.preload(operator)
        await seed_mod.seed_permissions(db)
        perm_ids = {p.id for p in db.rows_of(Permission)}
        granted = _rp_of(db, operator)
        expected = {
            p.id
            for p in db.rows_of(Permission)
            if f"{p.scope}:{p.resource}:{p.action}" in {spec.code for spec in BUILTIN_ROLE_PERMISSIONS["operator"]}
        }
        assert granted == expected
        assert granted <= perm_ids and granted

    async def test_grant_idempotent_no_dup(self):
        db = MiniDB()
        operator = Role(id=10, tenant_id=PLATFORM_TENANT_ID, name="运营", code="operator", is_builtin=True)
        db.preload(operator)
        await seed_mod.seed_permissions(db)
        n = len(_rp_of(db, operator))
        assert n == len(BUILTIN_ROLE_PERMISSIONS["operator"])
        await seed_mod.seed_permissions(db)
        assert len(_rp_of(db, operator)) == n

    async def test_missing_builtin_role_skipped_with_warning(self, caplog):
        db = MiniDB()  # 无任何角色行
        with caplog.at_level(logging.WARNING, logger="llmpod.permission.seed"):
            await seed_mod.seed_permissions(db)
        assert any("内置角色 operator 未找到" in r.message for r in caplog.records)
        assert db.rows_of(RolePermission) == []

    async def test_unregistered_perm_in_role_map_skipped_with_warning(self, caplog, monkeypatch):
        """角色映射引用了未注册权限 → 告警跳过（不产生脏 FK）。"""
        bogus = PermSpec("platform", "nope", "ghost", "不存在")
        patched = {"operator": [bogus]}
        monkeypatch.setattr("llmpod.permission.permissions.BUILTIN_ROLE_PERMISSIONS", patched)
        db = MiniDB()
        operator = Role(id=10, tenant_id=PLATFORM_TENANT_ID, name="运营", code="operator", is_builtin=True)
        db.preload(operator)
        with caplog.at_level(logging.WARNING, logger="llmpod.permission.seed"):
            await seed_mod.seed_permissions(db)
        assert any("platform:nope:ghost" in r.message for r in caplog.records)
        assert _rp_of(db, operator) == set()


class TestSeedTenantBuiltinRoles:
    async def test_creates_both_builtin_roles_with_grants(self):
        db = MiniDB()
        # 真实开通流程：租户域权限先经 seed_permissions upsert 进库，再建角色授权
        await seed_mod.seed_permissions(db)
        await seed_mod.seed_tenant_builtin_roles(db, 5)
        roles = {r.code: r for r in db.rows_of(Role)}
        assert set(roles) == {"tenant_admin", "tenant_operator"}
        assert all(r.tenant_id == 5 and r.is_builtin for r in roles.values())
        # 授权 = 租户域映射里的权限码全部可解析到已 upsert 的 Permission 行
        for code in ("tenant_admin", "tenant_operator"):
            granted = _rp_of(db, roles[code])
            assert len(granted) == len(BUILTIN_ROLE_PERMISSIONS[code])

    async def test_idempotent_second_run(self):
        db = MiniDB()
        await seed_mod.seed_permissions(db)
        await seed_mod.seed_tenant_builtin_roles(db, 5)
        before = sorted((r.code, r.id) for r in db.rows_of(Role))
        n_rp = len(db.rows_of(RolePermission))
        await seed_mod.seed_tenant_builtin_roles(db, 5)
        assert sorted((r.code, r.id) for r in db.rows_of(Role)) == before
        assert len(db.rows_of(RolePermission)) == n_rp

    async def test_existing_tenant_role_reused_not_recreated(self):
        db = MiniDB()
        await seed_mod.seed_permissions(db)
        existing = Role(id=77, tenant_id=5, name="管理员", code="tenant_admin", is_builtin=True)
        db.preload(existing)
        await seed_mod.seed_tenant_builtin_roles(db, 5)
        admins = [r for r in db.rows_of(Role) if r.code == "tenant_admin"]
        assert len(admins) == 1 and admins[0].id == 77 and admins[0].name == "管理员"
        # 缺的 tenant_operator 随建随授权
        ops = [r for r in db.rows_of(Role) if r.code == "tenant_operator"]
        assert len(ops) == 1
        assert len(_rp_of(db, ops[0])) == len(BUILTIN_ROLE_PERMISSIONS["tenant_operator"])

    async def test_tenants_isolated(self):
        db = MiniDB()
        await seed_mod.seed_permissions(db)
        await seed_mod.seed_tenant_builtin_roles(db, 5)
        await seed_mod.seed_tenant_builtin_roles(db, 6)
        assert {r.tenant_id for r in db.rows_of(Role)} == {5, 6}
        assert len([r for r in db.rows_of(Role) if r.tenant_id == 6]) == 2

    async def test_grant_skipped_when_tenant_perms_not_seeded(self, caplog):
        """权限未 upsert（未跑 seed_permissions）→ 授权告警跳过，不建脏 FK。"""
        db = MiniDB()
        with caplog.at_level(logging.WARNING, logger="llmpod.permission.seed"):
            await seed_mod.seed_tenant_builtin_roles(db, 5)
        roles = [r for r in db.rows_of(Role) if r.code == "tenant_admin"]
        assert len(roles) == 1  # 角色仍创建
        assert _rp_of(db, roles[0]) == set()  # 授权全跳过
        assert any("未注册，无法授权" in r.message for r in caplog.records)


class TestCheckConsistency:
    def test_clean_registry_returns_empty(self):
        assert seed_mod.check_consistency() == []

    def test_role_map_referencing_unregistered_perm_warned(self, monkeypatch):
        bogus = PermSpec("platform", "ghost", "res", "")
        patched = dict(BUILTIN_ROLE_PERMISSIONS)
        patched["operator"] = [bogus]
        monkeypatch.setattr("llmpod.permission.permissions.BUILTIN_ROLE_PERMISSIONS", patched)
        warnings = seed_mod.check_consistency()
        assert len(warnings) == 1
        assert "角色 operator" in warnings[0] and "platform:ghost:res" in warnings[0]

    def test_menu_referencing_unregistered_perm_warned(self, monkeypatch):
        bogus = PermSpec("tenant", "ghost", "res", "")
        spec = MenuSpec(path="/tenant/ghost", title="幻影", scope="tenant", required=(bogus,))
        monkeypatch.setattr(
            "llmpod.permission.menu.menus_by_scope",
            lambda scope: [spec] if scope == "tenant" else [],
        )
        warnings = seed_mod.check_consistency()
        assert len(warnings) == 1
        assert "/tenant/ghost" in warnings[0] and "tenant:ghost:res" in warnings[0]
