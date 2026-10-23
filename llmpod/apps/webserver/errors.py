"""统一 API 错误处理：将 LLMPod 领域异常映射为标准 HTTP 响应。

领域异常体系见 llmpod.core.exceptions，路由/service 只需 raise 对应类型，
本模块负责把异常映射到合适的 HTTP status 与 Result 格式 body。

约定:
  code=200  正常
  code=400  通用业务错误（LLMPodError 兜底）
  code=401  AuthError
  code=403  PermissionDenied
  code=404  NotFound
  code=409  Conflict
  code=422  LLMPodValidationError / 请求参数校验失败
  code=503  Maintenance（维护模式）
  code=500  服务器内部错误（未捕获异常）

body 额外携带 ``error_code``：稳定的业务错误码字符串（见 ``ErrorCode``），
同一 HTTP status 下可区分子类型（如 401 下 UNAUTHENTICATED / TOKEN_EXPIRED /
INVALID_CREDENTIALS）。前端 ``switch error_code`` 分支，不碰 message。
成功响应 error_code=null，旧前端不受影响。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.exc import IntegrityError

from llmpod.apps.webserver.schemas.response import error
from llmpod.core.exceptions import (
    DEFAULT_MAINTENANCE_MESSAGE,
    ErrorCode,
    LLMPodError,
    AuthError,
    Maintenance,
    PermissionDenied,
    NotFound,
    Conflict,
    LLMPodValidationError,
)

log = logging.getLogger("llmpod.apps.webserver.errors")


def _result(
    status_code: int,
    message: str,
    data: Any = None,
    error_code: str | None = None,
) -> JSONResponse:
    """构建统一错误响应 body（委托 :func:`error` 复用 ``ErrorResult`` 唯一定义处）。"""
    return JSONResponse(
        status_code=status_code,
        content=error(message=message, code=status_code, data=data, error_code=error_code),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，统一所有响应为 Result 格式。

    - 请求校验错误 (422) → code=422, data=校验详情
    - HTTPException → 透传 status_code，body 转 Result
    - LLMPodError 各子类 → 按语义映射到 401/403/404/409/422/400
    - 其他未捕获异常 → code=500

    注意：apps/node 等专属异常（TaskExecutionError/NodeConnectionError）跨进程调用时
    由 tunnel 协议层序列化为 RemoteError，不会以原异常类型到达 webserver，故此处不处理。
    """

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        """FastAPI 请求参数校验失败：统一转为 code=422。"""
        errors = [
            {
                "loc": list(e.get("loc", [])),
                "msg": e.get("msg", ""),
                "type": e.get("type", ""),
            }
            for e in exc.errors()
        ]
        return _result(422, "请求参数校验失败", errors, ErrorCode.VALIDATION_ERROR.value)

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError):
        # 实例级 error_code：UNAUTHENTICATED / TOKEN_EXPIRED / INVALID_CREDENTIALS
        return _result(401, str(exc) or "认证失败", error_code=exc.error_code.value)

    @app.exception_handler(PermissionDenied)
    async def permission_denied_handler(request: Request, exc: PermissionDenied):
        data = (
            {"scope": exc.scope, "resource": exc.resource, "action": exc.action}
            if exc.scope or exc.resource or exc.action
            else None
        )
        return _result(403, str(exc) or "权限不足", data, exc.error_code.value)

    @app.exception_handler(NotFound)
    async def not_found_handler(request: Request, exc: NotFound):
        data = {"resource": exc.resource, "resource_id": exc.resource_id} if exc.resource else None
        return _result(404, str(exc) or "资源不存在", data, exc.error_code.value)

    @app.exception_handler(Conflict)
    async def conflict_handler(request: Request, exc: Conflict):
        # Conflict 可携带结构化详情（如 start 端点的「原卡被占用」明细）供前端展示
        details = getattr(exc, "details", None)
        return _result(409, str(exc) or "资源冲突", data=details, error_code=exc.error_code.value)

    @app.exception_handler(LLMPodValidationError)
    async def validation_error_handler(request: Request, exc: LLMPodValidationError):
        return _result(422, str(exc) or "输入校验失败", error_code=exc.error_code.value)

    @app.exception_handler(Maintenance)
    async def maintenance_handler(request: Request, exc: Maintenance):
        # 维护模式拒绝：503 + MAINTENANCE 码，message 为维护文案（前端按码分支）
        return _result(503, str(exc) or DEFAULT_MAINTENANCE_MESSAGE, error_code=exc.error_code.value)

    @app.exception_handler(LLMPodError)
    async def llmpod_error_handler(request: Request, exc: LLMPodError):
        """LLMPodError 兜底（含 ConfigError 及各 app 自定义的 LLMPodError 子类）。

        error_code 取实例码（子类可覆盖类属性 / 实例可经 ``error_code=`` 覆盖），
        默认 BUSINESS_ERROR → 400。例外：``FINALIZE_FAILED`` 是合并期服务端失败，
        映射 500（其余业务码仍 400）。
        """
        status_code = 500 if exc.error_code == ErrorCode.FINALIZE_FAILED else 400
        return _result(status_code, str(exc) or "业务错误", error_code=exc.error_code.value)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """HTTPException（FastAPI 内置 404 等）：透传 status_code，无对应业务码。"""
        detail = exc.detail if isinstance(exc.detail, str) else "error"
        return _result(exc.status_code, detail)

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        """DB 约束违例（unique / not-null / check / fk）→ 领域级 Result，不裸 500。

        修复「用户输入触发约束违例 → 笼统 500 INTERNAL_ERROR」的泄漏：前端拿到稳定的 409/422 + error_code。
        asyncpg orig 类型：UniqueViolationError / NotNullViolationError / CheckViolationError / ForeignKeyViolationError。
        not-null / check → 422 校验失败；unique / fk → 409 冲突。
        """
        orig = getattr(exc, "orig", None)
        orig_name = type(orig).__name__ if orig is not None else type(exc).__name__
        raw = str(orig) if (orig and str(orig)) else str(exc)
        detail = raw.splitlines()[0] if raw else ""
        is_validation = "NotNull" in orig_name or "Check" in orig_name
        # 原始约束名/原文可能带表结构（asyncpg 报错含列名/约束名），只在日志留痕，
        # 不回传客户端——data 只给分类与中文说明
        if is_validation:
            log.warning("IntegrityError(validation) %s %s [%s] %s", request.method, request.url.path, orig_name, detail)
            return _result(422, "数据校验失败", {"kind": "validation"}, ErrorCode.VALIDATION_ERROR.value)
        log.warning("IntegrityError(conflict) %s %s [%s] %s", request.method, request.url.path, orig_name, detail)
        return _result(409, "资源冲突（违反唯一/外键约束）", {"kind": "conflict"}, ErrorCode.CONFLICT.value)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        """未捕获异常：code=500，记录完整堆栈。"""
        log.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return _result(500, "服务器内部错误", error_code=ErrorCode.INTERNAL_ERROR.value)
