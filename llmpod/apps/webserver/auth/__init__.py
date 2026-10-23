"""WebServer 认证与鉴权依赖。"""

from llmpod.apps.webserver.auth.deps import (
    bearer_scheme,
    get_current_user,
    require_platform_role,
    require_tenant_access,
    current_tenant_id,
    require_authenticated,
    require_platform_or_tenant_access,
    require_permission,
)

__all__ = [
    "bearer_scheme",
    "get_current_user",
    "require_platform_role",
    "require_tenant_access",
    "current_tenant_id",
    "require_authenticated",
    "require_platform_or_tenant_access",
    "require_permission",
]
