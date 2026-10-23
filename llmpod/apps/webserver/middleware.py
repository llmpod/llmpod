"""HTTP 中间件。"""

from __future__ import annotations

import logging
import time

import starlette.middleware.gzip as _starlette_gzip
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("llmpod.apps.webserver.middleware")


# 扩展 starlette GZipMiddleware 的流式响应排除清单。
#
# starlette 的 GZipMiddleware 内置按响应 content-type 排除压缩的机制
# （content_type_is_excluded），但默认排除列表 DEFAULT_EXCLUDED_CONTENT_TYPES
# 只含 text/event-stream。NDJSON 长流（tail-log = docker logs -f、docker-pull
# 逐行输出）与 SSE 同属「不该 gzip 的逐帧流式响应」：gzip 用 GzipFile 累积
# chunk，只在响应结束（more_body=False）close 时才 flush，而流式响应的
# more_body 恒为 True，帧数据会滞留 zlib 缓冲直到连接关闭才一次性吐出——浏览器
# 强制带 Accept-Encoding: gzip（forbidden header，JS 无法改写），前端容器日志
# 面板因此永远收不到帧。
#
# 把 application/x-ndjson 纳入排除，复用 starlette 官方的逐块透传逻辑，
# 新增 NDJSON/SSE 流式端点无需改任何代码。starlette 未把排除列表做成构造
# 参数（社区已提需求未合入），只能在模块级补丁扩展。
#
# application/octet-stream（模型分发 FileResponse 的默认 content-type）同样排除：
# 模型权重是高熵二进制，gzip 压缩比≈1 却逐 chunk 烧 CPU——分发大模型时 manager
# 单核被打满、吞吐从盘速跌到百 KB/s 级（实测 45~144KB/s），900s 部署预算必超时。
# 客户端（node httpx）默认带 Accept-Encoding: gzip，不吃排除清单就必被压缩。
#
# 防御性检查：升级 starlette 若改名/删该常量，此处直接抛错（fail-fast），
# 绝不静默回退到 gzip 滞留的坏行为。
if not hasattr(_starlette_gzip, "DEFAULT_EXCLUDED_CONTENT_TYPES"):
    raise RuntimeError(
        "starlette 版本不含 DEFAULT_EXCLUDED_CONTENT_TYPES：gzip 流式排除未生效。"
        "请检查 starlette 版本或改用自定义中间件"
    )
_starlette_gzip.DEFAULT_EXCLUDED_CONTENT_TYPES = (
    "text/event-stream",
    "application/x-ndjson",
    "application/octet-stream",
)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """记录请求方法、路径、状态码和耗时。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000

        # 跳过健康检查与静态资源日志噪音（多 SPA：根 /assets/ 与 /admin|tenant/.../assets/）
        path = request.url.path
        if path != "/health" and "/assets/" not in path:
            log.info(
                "%s %s → %d (%.1fms)",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
            )
        return response


# 维护闸豁免前缀：这些 /api/v1 路径不受 maintenanceMode 影响。
# - auth/login|logout：被拦用户要能登录看到维护提示、能干净退出
# - settings：公开读（登录页维护横幅数据源）
# - nodes / distribution：节点引导与模型分发走下载令牌鉴权（节点隧道语义，维护期不停）
_MAINTENANCE_EXEMPT_PREFIXES = (
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/settings",
    "/api/v1/nodes",
    "/api/v1/distribution",
)


class MaintenanceGateMiddleware(BaseHTTPMiddleware):
    """维护模式闸：maintenanceMode 开启时，非平台侧的已登录 API 会话一律 401。

    与登录拦截（public/auth.py 拒非平台域登录）互补，覆盖存量会话——
    开启维护后不要求（也无法）等存量 token 自然过期。判定顺序按成本递增：
    命中 /api/v1 且带 Bearer token 才查维护态（进程内 5s TTL 缓存），
    维护开启才查 Redis 会话判是否平台侧。匿名请求放行交由鉴权层照常 401；
    非 /api/v1 路径（SPA/静态/docs/health/WS 隧道）不经本闸。
    判定链任何内部异常（DB/Redis 不可达）一律 fail-open 放行——维护开关
    读取故障绝不打死整个 API。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if not path.startswith("/api/v1/") or path.startswith(_MAINTENANCE_EXEMPT_PREFIXES):
            return await call_next(request)

        # Bearer 解析与鉴权链同口径（auth.deps._extract_token），惰性导入避免环
        from llmpod.apps.webserver.auth.deps import _extract_token

        token = _extract_token(request)
        if not token:
            return await call_next(request)

        # 只包判定段：call_next 进 try 会把业务端点的异常也当「闸判定失败」，
        # 打误导日志后把下游链重跑第二遍（副作用翻倍）。判定分两级、失败语义不同：
        # - 维护态本身读不到（DB 抖动）→ 放行：拦会把未开维护的平台整体打死；
        # - 维护已开启、豁免判定失败（Redis 抖动）→ fail-closed 拦截：维护是
        #   显式开启的运营状态，宁可在窗口内多拦非平台会话（前端按 401 跳登录），
        #   不能把维护中的系统在依赖抖动时整段放行给普通用户
        try:
            state = await self._maintenance_state()
        except Exception:
            log.warning("maintenance gate 维护态不可读，放行", exc_info=True)
            return await call_next(request)
        if not state.enabled:
            return await call_next(request)
        try:
            exempt = await self._session_exempt_from_maintenance(token)
        except Exception:
            log.warning("maintenance gate 豁免判定失败，按维护中拦截（fail-closed）", exc_info=True)
            return self._maintenance_401(state.message)
        if exempt:
            return await call_next(request)

        # 401（而非登录拦截的 503）是刻意的：前端 http-client 的
        # errorFeedbackMiddleware 只对 status===401 清 token + 整页跳 /login，
        # 503 只弹一条 toast 把用户留在原页继续报错。error_code 仍打 MAINTENANCE，
        # 与真正的凭据失效（TOKEN_EXPIRED/UNAUTHENTICATED）区分开。
        return self._maintenance_401(state.message)

    @staticmethod
    def _maintenance_401(message: str):
        """维护拦截响应（正常拦截与 fail-closed 兜底共用）。"""
        from llmpod.apps.webserver.errors import _result
        from llmpod.core.exceptions import ErrorCode

        return _result(401, message, error_code=ErrorCode.MAINTENANCE.value)

    @staticmethod
    async def _maintenance_state():
        """当前维护态（settings_service 进程内 TTL 缓存，5s 粒度生效）。"""
        from llmpod.db.pg import get_db_context
        from llmpod.services.settings_service import get_maintenance

        async with get_db_context() as db:
            return await get_maintenance(db)

    @staticmethod
    async def _session_exempt_from_maintenance(token: str) -> bool:
        """按 Bearer token 查 Redis 会话，判其是否豁免维护闸。

        豁免口径与登录拦截（routes/public/auth.py）同源：``SessionData.exempt_from_maintenance``
        （超管/平台域）——只豁免超管会让平台域 operator 陷入「登录放行 → 首调被踢回
        登录页 → 再登录放行」无限跳转。会话缺失/损坏一律 False（不豁免），与
        「维护期不暴露 token 有效性」一致。
        """
        from llmpod.apps.webserver.app import get_settings
        from llmpod.apps.webserver.auth.session import SessionData
        from llmpod.core.security import hash_token
        from llmpod.db.redis import get_redis

        redis = get_redis()
        app_settings = get_settings()
        session_json = await redis.get(f"{app_settings.token_key_prefix}{hash_token(token)}")
        if not session_json:
            return False
        try:
            session = SessionData.model_validate_json(session_json)
        except Exception:
            return False
        return session.exempt_from_maintenance
