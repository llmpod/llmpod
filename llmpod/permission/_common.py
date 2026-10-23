"""权限声明的共用基建：``PermSpec`` 工厂、scope 常量、角色映射聚合。

平台端 / 租户端的具体权限常量分别在 :mod:`llmpod.permission.platform` 与
:mod:`llmpod.permission.tenant` 声明（导入即自登记进 :mod:`llmpod.permission.registry`），
两侧各自维护“内置角色 → 权限”映射；:mod:`llmpod.permission.permissions` 调用本模块的
:func:`build_builtin_role_permissions` 把两侧映射合并为全局 ``BUILTIN_ROLE_PERMISSIONS``，
供 seed 与一致性检查消费。

之所以拆出共用层：``_r`` 工厂与 scope 常量被两侧共用，集中一处避免重复定义漂移；
角色映射合并逻辑跨两域，放在比 platform/tenant 更上层的位置。
"""

from __future__ import annotations

from llmpod.permission.registry import PermSpec, register_permission

# scope 常量：区分管理端 / 租户端
SCOPE_PLATFORM = "platform"
SCOPE_TENANT = "tenant"


def _r(scope: str, resource: str, action: str, description: str = "") -> PermSpec:
    """构造一条权限常量并登记进注册表，返回该 ``PermSpec``。"""
    return register_permission(PermSpec(scope, resource, action, description))


def build_builtin_role_permissions(
    platform_roles: dict[str, list[PermSpec]],
    tenant_roles: dict[str, list[PermSpec]],
) -> dict[str, list[PermSpec]]:
    """合并平台端与租户端内置角色映射为全局映射。

    角色 code 在两域不会重复（平台域角色挂 tenant_id=0、租户域角色挂具体租户），
    故直接 dict 合并即可。
    """
    return {**platform_roles, **tenant_roles}
