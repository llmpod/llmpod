"""API 网关路由模块（/v1/* 对外契约 + /health）。"""

from llmpod.apps.proxyserver.routes.inference import router

__all__ = ["router"]
