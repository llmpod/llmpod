"""管理员平台路由聚合。

设计要点：
1. 统一前缀 `/admin`，子模块只写业务前缀（/clusters, /nodes, ...）。
2. 通过 `dependencies=[Depends(require_platform_role)]` 在本层做一次性鉴权，
   子路由的 handler 不再需要逐个写 `user: SessionData = Depends(require_platform_role)`。
3. 需要 user 信息的 handler，改用 `user: SessionData = Depends(get_current_user)`
   ——FastAPI 对同一请求内的同一依赖会缓存结果，require_platform_role 内部
   已调用过 get_current_user，这里再次声明不会重复执行。
"""

from fastapi import APIRouter, Depends

from llmpod.apps.webserver.auth.deps import require_platform_role

from .access import router as access_router
from .cluster import router as cluster_router
from .node import router as node_router
from .gpu import router as gpu_router
from .system import router as system_router
from .settings import router as settings_router
from .orchestration import router as orchestration_router
from .users import router as users_router
from .tenants import router as tenants_router
from .model import model_router
from .deployment import deployment_router
from .uploads import router as uploads_router
from .inference_image import router as inference_image_router
from .api_keys import router as api_keys_router
from .endpoints import router as endpoints_router
from .publishes import router as publishes_router
from .request_logs import router as request_logs_router
from .dashboard import router as dashboard_router
from .audit_logs import router as audit_logs_router
from .wallet import router as wallet_router, wallets_router
from .inference import router as inference_router
from .models_market import router as models_market_router

router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_platform_role)],
)

# —— 基础设施层 ——
router.include_router(cluster_router, prefix="/clusters", tags=["Admin - Clusters"])
router.include_router(node_router, prefix="/nodes", tags=["Admin - Nodes"])
router.include_router(gpu_router, prefix="/gpus", tags=["Admin - GPUs"])

# —— 平台运营层 ——
router.include_router(users_router, prefix="/users", tags=["Admin - Users"])
router.include_router(tenants_router, prefix="/tenants", tags=["Admin - Tenants"])

# —— 模型与部署层（推理部署闭环：部署生命周期 + 部署日志均挂 /model-deployments）——
router.include_router(model_router, prefix="/models", tags=["Admin - Models"])
router.include_router(deployment_router, prefix="/model-deployments", tags=["Admin - Model Deployments"])

# —— 通用切片上传层（与模型解耦：/admin/uploads/*）——
router.include_router(uploads_router, prefix="/uploads", tags=["Admin - Uploads"])

# —— 任务编排层 ——
router.include_router(orchestration_router, prefix="/orchestration", tags=["Admin - Orchestration"])

# —— 系统层 ——
router.include_router(system_router, prefix="/system", tags=["Admin - System"])
router.include_router(settings_router, prefix="/settings", tags=["Admin - Settings"])

# —— 推理镜像目录 ——
router.include_router(inference_image_router, prefix="/inference-images", tags=["Admin - Inference Images"])

# —— 权限管理 ——
router.include_router(access_router, prefix="/access", tags=["Admin - Access"])

# —— 网关配置态（ProxyServer：Key 签发/发布管理/租户授权）——
router.include_router(api_keys_router, prefix="/api-keys", tags=["Admin - API Keys"])
router.include_router(endpoints_router, prefix="/endpoints", tags=["Admin - Endpoints"])
router.include_router(publishes_router, prefix="/publishes", tags=["Admin - Publishes"])
router.include_router(models_market_router, prefix="/models-market", tags=["Admin - Model Market"])
router.include_router(request_logs_router, prefix="/request-logs", tags=["Admin - Request Logs"])
router.include_router(dashboard_router, prefix="/dashboard", tags=["Admin - Dashboard"])
router.include_router(audit_logs_router, prefix="/audit-logs", tags=["Admin - Audit Logs"])
router.include_router(wallet_router, prefix="/tenants/{tenant_id}/wallet", tags=["Admin - Wallet"])
router.include_router(wallets_router, prefix="/wallets", tags=["Admin - Wallet"])

# —— 在线调试转发（透传网关 proxyserver，平台管理员免 Key 直跑模型 API）——
router.include_router(inference_router, prefix="/inference", tags=["Admin - Inference"])

__all__ = ["router"]
