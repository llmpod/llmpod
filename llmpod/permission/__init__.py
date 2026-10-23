"""权限注册表与策略层。

- ``registry``：进程级权限注册表（``PermSpec`` 常量在声明模块导入时自登记）。
- ``platform``：平台域（管理端）权限常量 + 平台内置角色映射。
- ``tenant``：租户域（租户端）权限常量 + 租户内置角色映射。
- ``menu``：菜单声明注册表（纯声明非权限；``required`` 引用权限常量做可见性判定）。
- ``permissions``：两侧权限常量的聚合重导出 + 全局 ``BUILTIN_ROLE_PERMISSIONS``。
- ``seed``：``seed_permissions`` 幂等同步到 DB + ``check_consistency`` 启动一致性检查。
"""

# 先导入 platform/tenant 触发权限常量自登记，再导入聚合层
from llmpod.permission import platform, tenant  # noqa: F401 — 触发自登记
from llmpod.permission._common import SCOPE_PLATFORM, SCOPE_TENANT
from llmpod.permission.permissions import BUILTIN_ROLE_PERMISSIONS, register_all
from llmpod.permission.seed import check_consistency, seed_permissions, seed_tenant_builtin_roles
from llmpod.permission.registry import (
    PermSpec,
    is_registered,
    register_permission,
    registered_permissions,
    _reset_for_test,
)

__all__ = [
    "PermSpec",
    "register_permission",
    "registered_permissions",
    "is_registered",
    "_reset_for_test",
    "register_all",
    "BUILTIN_ROLE_PERMISSIONS",
    "seed_permissions",
    "seed_tenant_builtin_roles",
    "check_consistency",
    "SCOPE_PLATFORM",
    "SCOPE_TENANT",
]
