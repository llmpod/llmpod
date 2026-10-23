"""权限注册表单元测试：register_permission 幂等/补全、registered_permissions 排序、
permissions 模块常量自登记。

注意：注册表是进程级全局，测试用唯一前缀 ``testfoo`` 避免与真实权限碰撞；
并在每个测试前 ``_reset_for_test`` 清空、``register_all`` 重建，保证隔离。
"""

from __future__ import annotations

import pytest

import llmpod.permission.platform
import llmpod.permission.tenant
from llmpod.permission.permissions import register_all
from llmpod.permission.registry import (
    PermSpec,
    _reset_for_test,
    is_registered,
    register_permission,
    registered_permissions,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个测试前清空注册表并重建权限常量，避免跨用例污染。

    ``register_all`` 依赖 platform/tenant 模块的 ``_r`` 副作用登记；但测试清空注册表后
    那些模块已 import 缓存、不会再跑顶层登记，故这里显式重新 import 两个子模块，
    让 ``_r(...)`` 重新执行登记，再调用 ``register_all`` 补登聚合清单。
    """
    import importlib

    _reset_for_test()
    importlib.reload(llmpod.permission.platform)
    importlib.reload(llmpod.permission.tenant)
    register_all()
    yield
    _reset_for_test()
    importlib.reload(llmpod.permission.platform)
    importlib.reload(llmpod.permission.tenant)
    register_all()


class TestRegisterPermission:
    def test_register_new(self):
        spec = register_permission(PermSpec("platform", "testfoo", "create", "创建"))
        assert spec.code == "platform:testfoo:create"
        assert spec.description == "创建"
        assert is_registered("platform", "testfoo", "create")

    def test_idempotent_no_duplicate(self):
        register_permission(PermSpec("platform", "testfoo", "read"))
        register_permission(PermSpec("platform", "testfoo", "read"))
        register_permission(PermSpec("platform", "testfoo", "read", "描述"))
        codes = [p.code for p in registered_permissions()]
        assert codes.count("platform:testfoo:read") == 1

    def test_first_description_wins(self):
        # 首次带 description，后调用空 description 不覆盖
        register_permission(PermSpec("platform", "testfoo", "update", "首次描述"))
        register_permission(PermSpec("platform", "testfoo", "update", ""))
        spec = next(p for p in registered_permissions() if p.code == "platform:testfoo:update")
        assert spec.description == "首次描述"

    def test_empty_description_backfilled(self):
        # 首次空 description，后调用带 description 则补全
        register_permission(PermSpec("platform", "testfoo", "delete"))
        register_permission(PermSpec("platform", "testfoo", "delete", "补全描述"))
        spec = next(p for p in registered_permissions() if p.code == "platform:testfoo:delete")
        assert spec.description == "补全描述"

    def test_later_description_does_not_overwrite_existing(self):
        register_permission(PermSpec("platform", "testfoo", "read", "旧描述"))
        register_permission(PermSpec("platform", "testfoo", "read", "新描述"))
        spec = next(p for p in registered_permissions() if p.code == "platform:testfoo:read")
        assert spec.description == "旧描述"


class TestRegisteredPermissionsSorted:
    def test_sorted_by_scope_resource_action(self):
        register_permission(PermSpec("tenant", "zzz", "b"))
        register_permission(PermSpec("platform", "aaa", "y"))
        register_permission(PermSpec("platform", "aaa", "x"))
        codes = [p.code for p in registered_permissions() if p.resource in ("aaa", "zzz")]
        # platform 先于 tenant；同 scope 内按 resource, action（字符串序）
        assert codes == ["platform:aaa:x", "platform:aaa:y", "tenant:zzz:b"]


class TestPermissionsModuleRegistersAll:
    """permissions 模块导入/register_all 后，已知常量应已登记。"""

    def test_known_constants_registered(self):
        from llmpod.permission.permissions import (
            ACCESS_READ,
            ADMIN_USER_READ,
            CLUSTER_READ,
            TENANT_USER_READ,
        )

        for perm in (CLUSTER_READ, ACCESS_READ, ADMIN_USER_READ, TENANT_USER_READ):
            assert is_registered(perm.scope, perm.resource, perm.action)

    def test_all_constants_have_description(self):
        # 每条已登记权限都应带非空 description（否则 web 展示空白）
        for spec in registered_permissions():
            assert spec.description, f"权限 {spec.code} 缺少 description"

    def test_split_perms_distinct_codes(self):
        # user:read 在平台/租户两侧是不同权限码
        from llmpod.permission.permissions import ADMIN_USER_READ, TENANT_USER_READ

        assert ADMIN_USER_READ.code != TENANT_USER_READ.code
        assert ADMIN_USER_READ.resource == TENANT_USER_READ.resource == "user"
        assert ADMIN_USER_READ.action == TENANT_USER_READ.action == "read"
        assert ADMIN_USER_READ.scope != TENANT_USER_READ.scope
