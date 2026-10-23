"""公开路由聚合：无需登录的端点（登录、健康探测等）。

注意：登出 / 获取当前用户等需要 token 但不限定 access_domain 的端点，
建议放在 `routes/shared/` 下挂载 require_authenticated；此处只放真正的公开端点。
"""

from fastapi import APIRouter

from .auth import router as auth_router
from .settings import router as settings_router
from .version import router as version_router
from .distribution import router as distribution_router
from .node import router as node_router

router = APIRouter()
router.include_router(auth_router, prefix="/auth", tags=["Public - Authentication"])
router.include_router(settings_router, prefix="/settings", tags=["Public - Settings"])
router.include_router(version_router, prefix="/version", tags=["Public - Version"])
router.include_router(node_router, prefix="/nodes", tags=["Public - Node Bootstrap"])
router.include_router(distribution_router, prefix="/distribution", tags=["Public - Model Distribution"])

__all__ = ["router"]
