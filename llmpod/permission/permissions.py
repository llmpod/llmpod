"""权限声明聚合层：平台端 + 租户端权限常量的单一对外入口。

具体权限常量分别声明在 :mod:`llmpod.permission.platform`（平台域）与
:mod:`llmpod.permission.tenant`（租户域），两侧各自导入即自登记进
:mod:`llmpod.permission.registry`，并各自维护“内置角色 → 权限”映射。本模块仅做两件事：

1. 重导出全部权限常量，使外部调用方无需感知平台/租户的物理拆分——
   ``from llmpod.permission.permissions import CLUSTER_READ`` 等既有用法不变；
2. 聚合两侧内置角色映射为全局 ``BUILTIN_ROLE_PERMISSIONS``，供 seed 与一致性检查消费。

权限模型为 ``scope:resource:action`` 三元组：

- ``scope`` ∈ ``platform`` | ``tenant``，区分管理端 / 租户端操作权限。仅跨域出现的
  resource（``user``、``tenant``）拆成平台/租户两条常量；平台独占 resource
  （cluster/node/gpu/system/access）只声明 ``scope=platform``，不造租户变体。
- ``resource`` 为资源名，``action`` 为操作名。菜单不是权限（见
  :mod:`llmpod.permission.menu`）：入口可见性由菜单声明的 ``required`` 功能权限
  判定，``/auth/menus`` 据此把可见菜单的路径字典（path → 开关标注）下发给前端。
- ``super_admin`` 靠 ``is_superuser`` 会话短路，无需逐条授权，故不在
  ``BUILTIN_ROLE_PERMISSIONS`` 中列出。
"""

from __future__ import annotations

from llmpod.permission.platform import (
    ACCESS_READ,
    ACCESS_UPDATE,
    ADMIN_USER_CREATE,
    ADMIN_USER_DELETE,
    ADMIN_USER_READ,
    ADMIN_USER_UPDATE,
    API_KEY_CREATE,
    API_KEY_READ,
    API_KEY_UPDATE,
    SETTINGS_READ,
    SETTINGS_UPDATE,
    AUDIT_LOG_READ,
    CLUSTER_CREATE,
    CLUSTER_DELETE,
    CLUSTER_READ,
    CLUSTER_UPDATE,
    GPU_READ,
    INFERENCE_IMAGE_READ,
    INFERENCE_DEBUG,
    MODEL_CREATE,
    MODEL_DEPLOYMENT_CREATE,
    MODEL_DEPLOYMENT_DELETE,
    MODEL_DEPLOYMENT_READ,
    MODEL_DEPLOYMENT_UPDATE,
    MODEL_READ,
    MODEL_UPDATE,
    MODEL_DELETE,
    ENDPOINT_READ,
    ENDPOINT_UPDATE,
    PUBLISH_READ,
    PUBLISH_UPDATE,
    REQUEST_LOG_READ,
    WALLET_READ,
    WALLET_UPDATE,
    UPLOAD_WRITE,
    DASHBOARD_READ,
    NODE_BOOTSTRAP,
    NODE_DELETE,
    NODE_EXEC,
    NODE_READ,
    NODE_UPDATE,
    PLATFORM_READONLY_OPS,
    PLATFORM_TENANT_CREATE,
    PLATFORM_TENANT_DELETE,
    PLATFORM_TENANT_READ,
    PLATFORM_TENANT_UPDATE,
    ROLE_PERMISSIONS as _PLATFORM_ROLE_PERMISSIONS,
    SYSTEM_READ,
    SYSTEM_UPDATE,
)
from llmpod.permission.registry import PermSpec, register_permission
from llmpod.permission.tenant import (
    ROLE_PERMISSIONS as _TENANT_ROLE_PERMISSIONS,
    TENANT_ACCESS_READ,
    TENANT_ACCESS_UPDATE,
    TENANT_API_KEY_CREATE,
    TENANT_API_KEY_READ,
    TENANT_API_KEY_UPDATE,
    TENANT_AUDIT_LOG_READ,
    TENANT_DASHBOARD_READ,
    TENANT_MODEL_READ,
    TENANT_REQUEST_LOG_READ,
    TENANT_TENANT_READ,
    TENANT_USER_CREATE,
    TENANT_USER_DELETE,
    TENANT_USER_READ,
    TENANT_USER_UPDATE,
    TENANT_WALLET_READ,
)

from llmpod.permission._common import build_builtin_role_permissions

# 全局“内置角色 → 权限”映射：平台域角色（tenant_id=0）与租户域角色（具体租户）code 不重叠，直接合并。
BUILTIN_ROLE_PERMISSIONS: dict[str, list[PermSpec]] = build_builtin_role_permissions(
    _PLATFORM_ROLE_PERMISSIONS,
    _TENANT_ROLE_PERMISSIONS,
)

# 全部权限常量清单（供 register_all 重建注册表使用，与两侧声明一一对应）
_ALL_PERMS: list[PermSpec] = [
    CLUSTER_READ,
    CLUSTER_CREATE,
    CLUSTER_UPDATE,
    CLUSTER_DELETE,
    NODE_READ,
    NODE_UPDATE,
    NODE_EXEC,
    NODE_DELETE,
    NODE_BOOTSTRAP,
    GPU_READ,
    INFERENCE_IMAGE_READ,
    INFERENCE_DEBUG,
    MODEL_READ,
    MODEL_CREATE,
    MODEL_UPDATE,
    MODEL_DELETE,
    MODEL_DEPLOYMENT_READ,
    MODEL_DEPLOYMENT_CREATE,
    MODEL_DEPLOYMENT_UPDATE,
    MODEL_DEPLOYMENT_DELETE,
    API_KEY_READ,
    API_KEY_CREATE,
    API_KEY_UPDATE,
    ENDPOINT_READ,
    ENDPOINT_UPDATE,
    PUBLISH_READ,
    PUBLISH_UPDATE,
    REQUEST_LOG_READ,
    DASHBOARD_READ,
    AUDIT_LOG_READ,
    WALLET_READ,
    WALLET_UPDATE,
    UPLOAD_WRITE,
    SYSTEM_READ,
    SYSTEM_UPDATE,
    SETTINGS_READ,
    SETTINGS_UPDATE,
    ACCESS_READ,
    ACCESS_UPDATE,
    ADMIN_USER_READ,
    ADMIN_USER_CREATE,
    ADMIN_USER_UPDATE,
    ADMIN_USER_DELETE,
    TENANT_USER_READ,
    TENANT_USER_CREATE,
    TENANT_USER_UPDATE,
    TENANT_USER_DELETE,
    TENANT_ACCESS_READ,
    TENANT_ACCESS_UPDATE,
    TENANT_API_KEY_READ,
    TENANT_API_KEY_CREATE,
    TENANT_API_KEY_UPDATE,
    TENANT_REQUEST_LOG_READ,
    TENANT_AUDIT_LOG_READ,
    TENANT_WALLET_READ,
    TENANT_MODEL_READ,
    TENANT_DASHBOARD_READ,
    PLATFORM_TENANT_READ,
    PLATFORM_TENANT_CREATE,
    PLATFORM_TENANT_UPDATE,
    PLATFORM_TENANT_DELETE,
    TENANT_TENANT_READ,
]


def register_all() -> None:
    """重新登记全部权限常量进注册表。

    运行时无需调用——两侧模块首次导入时 ``_r(...)`` 已登记完毕。仅测试在
    ``_reset_for_test()`` 清空注册表后调用本函数重建注册表，避免 ``sys.modules``
    缓存导致二次 import 不重跑顶层登记。
    """
    for perm in _ALL_PERMS:
        register_permission(perm)
