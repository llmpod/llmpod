"""WebServer 应用：管理端的 REST API 路由。

导出 create_webserver_router() 和 webserver_lifespan 供管理端使用。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from llmpod.config.settings import Settings, settings

log = logging.getLogger("llmpod.apps.webserver")


class WebServerLifespan:
    """WebServer 生命周期管理句柄。"""

    def __init__(self):
        self.settings: Settings | None = None

    async def startup(self, settings: Settings):
        self.settings = settings
        log.info("WebServer started")
        # secret_key 未显式配置 → 首启随机生成并落 data/secret_key（0600，随数据卷
        # 持久化），此后每次启动复用。开源交付下 dev 默认 key（源码可读）不再兜底
        # 生产：API key 明文与 source_auth 的可逆加密每个部署拿到独立密钥。
        from llmpod.core.secrets import ensure_secret_key

        ensure_secret_key(settings)
        # 权限一致性检查：内置角色映射引用的权限码若未注册则告警，
        # 把"权限漂移"从运行时 403 提前到启动期。权限常量在 permissions 模块导入时
        # 自登记，webserver 路由已随 app 构建导入，登记已完成。
        from llmpod.permission.seed import check_consistency

        for warning in check_consistency():
            log.warning("权限一致性: %s", warning)

    async def shutdown(self):
        log.info("WebServer shutdown")


webserver_lifespan = WebServerLifespan()


def get_settings() -> Settings:
    """获取应用单例 Settings。"""
    if webserver_lifespan.settings is None:
        # 仅在测试或未初始化时回退到全局单例
        return settings
    return webserver_lifespan.settings


def create_webserver_router() -> APIRouter:
    """创建 WebServer 路由。

    采用三层聚合：每个子包（public/admin/tenant）在自己的 `__init__.py`
    中暴露已配置好前缀、tag、鉴权依赖的 router，本函数仅做顶层挂载。
    - public  : 无前缀，无登录
    - admin   : /admin/* ，统一 require_platform_role
    - tenant  : /tenant/*，统一 require_tenant_access
    - shared  : 跨域端点（管理员+租户均可），统一 require_platform_or_tenant_access
    """
    from llmpod.apps.webserver.routes.public import router as public_router
    from llmpod.apps.webserver.routes.admin import router as admin_router
    from llmpod.apps.webserver.routes.tenant import router as tenant_router
    from llmpod.apps.webserver.routes.shared import router as shared_router

    router = APIRouter()
    router.include_router(public_router)
    router.include_router(admin_router)
    router.include_router(tenant_router)
    router.include_router(shared_router)
    return router
