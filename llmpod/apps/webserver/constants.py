"""webserver 领域的常量（认证 / 账号 / 访问域）。

分层约定：**单 app 领域自用、不跨 app 引用的常量放各 app 自己的包内**，避免跨 app
依赖引入循环。真正被多个 app 共享的才放 ``config/constants.py``（全局）。

本文件放 web 后端自用的枚举：``AccountStatus``（user/tenant 账号状态）、
``AccessDomain`` + ``ACCESS_DOMAIN_MENU_SCOPE``（访问域与菜单侧映射）。消费面是
webserver 路由/auth 与 services/auth_service（web 认证后端），不触 manager/node。
"""

from enum import StrEnum


class AccountStatus(StrEnum):
    """user/tenant 共用的账号状态：active | disabled。"""

    ACTIVE = "active"  # 启用（可登录）
    DISABLED = "disabled"  # 停用（拒登录）


class AccessDomain(StrEnum):
    """user.access_domain：账号访问域（决定 RBAC 作用域与可见菜单侧）。"""

    PLATFORM = "platform"  # 平台域（super_admin/operator 等平台角色）
    TENANT = "tenant"  # 租户域（tenant_admin / 租户普通用户）
    TENANT_OBSERVER = "tenant_observer"  # 租户只读观察员


# 菜单域：按用户 access_domain 映射到权限 scope，决定可见菜单侧。
# 平台账号(platform)看 platform 菜单，租户账号（含只读观察员）看 tenant 菜单。
ACCESS_DOMAIN_MENU_SCOPE: dict[AccessDomain, str] = {
    AccessDomain.PLATFORM: "platform",
    AccessDomain.TENANT: "tenant",
    AccessDomain.TENANT_OBSERVER: "tenant",
}
