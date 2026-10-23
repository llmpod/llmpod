"""跨域路由聚合：平台端 + 租户端均可访问（require_platform_or_tenant_access 外层门禁）。

与 public（无登录）/ admin（仅平台）/ tenant（仅租户）并列；此处放「两端都要看」的
公共只读端点，不落任何单域。
"""

from fastapi import APIRouter, Depends

from llmpod.apps.webserver.auth.deps import require_platform_or_tenant_access

from .endpoint import router as endpoint_router

router = APIRouter(dependencies=[Depends(require_platform_or_tenant_access)])
router.include_router(endpoint_router, prefix="/proxy-endpoint", tags=["Shared - Proxy Endpoint"])

__all__ = ["router"]
