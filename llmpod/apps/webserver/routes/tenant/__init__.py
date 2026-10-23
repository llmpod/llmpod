"""租户平台路由聚合。

设计要点同 admin/__init__.py：统一 `/tenant` 前缀 + 一次性 require_tenant_access 鉴权。
"""

from fastapi import APIRouter, Depends

from llmpod.apps.webserver.auth.deps import require_tenant_access

from .users import router as users_router
from .profile import router as profile_router
from .access import router as access_router
from .api_keys import router as api_keys_router
from .request_logs import router as request_logs_router
from .audit_logs import router as audit_logs_router
from .wallet import router as wallet_router
from .models import router as models_router
from .dashboard import router as dashboard_router

router = APIRouter(
    prefix="/tenant",
    dependencies=[Depends(require_tenant_access)],
)

router.include_router(users_router, prefix="/users", tags=["Tenant - Users"])
router.include_router(profile_router, prefix="/profile", tags=["Tenant - Profile"])
router.include_router(access_router, prefix="/access", tags=["Tenant - Access"])
router.include_router(api_keys_router, prefix="/api-keys", tags=["Tenant - API Keys"])
router.include_router(request_logs_router, prefix="/request-logs", tags=["Tenant - Request Logs"])
router.include_router(audit_logs_router, prefix="/audit-logs", tags=["Tenant - Audit Logs"])
router.include_router(wallet_router, prefix="/wallet", tags=["Tenant - Wallet"])
router.include_router(models_router, prefix="/models", tags=["Tenant - Models"])
router.include_router(dashboard_router, prefix="/dashboard", tags=["Tenant - Dashboard"])

__all__ = ["router"]
