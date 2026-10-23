"""管理端统一入口: manager (WS) + webserver (REST) 融合。

单 FastAPI 应用在 8080 端口同时提供 WebSocket 和 REST API。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi

import llmpod
from llmpod.apps.webserver.frontend import mount_frontend
import llmpod.apps.webserver.middleware  # noqa: F401  扩展 GZip 流式排除清单（见模块注释）
from llmpod.apps.webserver.middleware import MaintenanceGateMiddleware, RequestLoggingMiddleware
from llmpod.apps.webserver.errors import register_exception_handlers
from llmpod.apps.webserver.schemas.response import ErrorResult
from llmpod.apps.manager.app import create_manager_router, manager_lifespan
from llmpod.apps.webserver.app import create_webserver_router, webserver_lifespan
from llmpod.config.settings import settings
from llmpod.core.logging import setup_logging
from llmpod.core.taskwatch import BackgroundTaskManager
from llmpod.db.lifecycle import init_all_dbs, close_all_dbs

logger = logging.getLogger("llmpod.control_plane")


@asynccontextmanager
async def control_plane_lifespan(app: FastAPI):
    """管理端统一生命周期: 数据库 + manager + webserver。"""
    setup_logging(settings.log_level)
    logger.info("LLMPod 管理端启动 version=%s", llmpod.__version__)

    # 初始化数据库（PG + Redis）
    await init_all_dbs(settings)
    background_tasks = BackgroundTaskManager()
    from llmpod.apps.webserver.metrics_scraper import start_metrics_scraper_task
    from llmpod.apps.webserver.services.upload import start_sweep_task
    from llmpod.services.telemetry_service import start_telemetry_task

    try:
        # Manager 恢复与 WebServer 初始化均先于后台任务登记。
        await manager_lifespan.startup(settings)
        await webserver_lifespan.startup(settings)
        await start_sweep_task(background_tasks)
        await start_metrics_scraper_task(background_tasks, settings)
        await start_telemetry_task(background_tasks)
        yield
    finally:
        await background_tasks.stop()
        await manager_lifespan.shutdown()
        await webserver_lifespan.shutdown()
        await close_all_dbs()


def create_webserver_app() -> FastAPI:
    """创建融合的管理端应用: manager(WS) + webserver(REST) 共用同一端口。"""
    # 根据配置控制文档端点
    docs_url = "/docs" if settings.docs_enabled else None
    redoc_url = "/redoc" if settings.docs_enabled else None
    openapi_url = "/openapi.json" if settings.docs_enabled else None

    app = FastAPI(
        title="LLMPod 管理端",
        description="LLMPod 管理 API",
        version=llmpod.__version__,
        lifespan=control_plane_lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    # 中间件按「后 add 者在外层」包裹。维护闸放最内层：它自己构造的响应也要
    # 流经访问日志与 CORS——否则跨域部署下浏览器直接拦掉维护响应，维护期请求
    # 也不进访问日志。（响应码与理由见 middleware.MaintenanceGateMiddleware）

    # 维护模式闸（maintenanceMode 开启时把非平台侧的已登录 API 会话踢回登录页；见模块注释）
    app.add_middleware(MaintenanceGateMiddleware)

    # 请求日志中间件
    app.add_middleware(RequestLoggingMiddleware)

    # CORS 跨域配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # GZip 压缩（前端 JS/CSS、API JSON 等较大响应）；NDJSON 流式响应已在
    # middleware 模块扩展 starlette 排除清单绕过（见模块注释）
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # 全局异常处理器：统一所有响应为 Result 格式（422→400, 500→500）
    register_exception_handlers(app)

    # 挂载 Manager 路由（WebSocket + 编排 API，内部已带 tags）
    manager_router = create_manager_router()
    app.include_router(manager_router)

    # 挂载 WebServer 路由（BFF REST API，统一前缀 /api/v1；tags 由内部子 router 提供）
    webserver_router = create_webserver_router()
    app.include_router(webserver_router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        """健康检查端点。"""
        return {"status": "ok", "service": "llmpod-control-plane", "version": llmpod.__version__}

    # 自定义 OpenAPI：移除自动生成的 422 响应，统一替换为 400
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        # 统一移除 422，添加 400 Result 响应
        for path, methods in schema.get("paths", {}).items():
            for method, info in methods.items():
                if method not in ("get", "post", "put", "delete", "patch"):
                    continue
                responses = info.get("responses", {})
                if "422" in responses:
                    del responses["422"]
                if "400" not in responses:
                    responses["400"] = {
                        "description": "主动错误（参数校验/业务失败）",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Result_Any_"}}},
                    }
        # 确保 Result_Any_ schema 存在：从 ErrorResult Pydantic 模型派生，
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        if "Result_Any_" not in components:
            components["Result_Any_"] = ErrorResult.model_json_schema()
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    # 挂载前端静态资源（含 SPA 兜底路由，必须最后注册，优先级低于上方所有业务路由）
    mount_frontend(app)

    return app


# 全局应用实例（uvicorn 引用此对象）
app = create_webserver_app()


async def main():
    """管理端主入口。"""
    config = uvicorn.Config(
        app,
        host=settings.webserver_host,
        port=settings.webserver_port,
        log_level=settings.log_level,
        access_log=False,  # 使用自定义中间件代替
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
