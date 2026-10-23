"""/metrics 端点鉴权（proxyserver 与管理端两 app 共用）。

fail-closed：``LLMPOD_METRICS_TOKEN`` 未配置（空串）时一律 401——宁可忘了配
拿不到指标，不可忘了配裸奔指标（标签含租户/部署维度信息）。比较用常数时间
函数，防时序侧信道（端口对外，这是唯一暴露面）。
"""

from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse


def verify_metrics_token(request: Request, token: str) -> JSONResponse | None:
    """校验 Authorization: Bearer <token>；返回 None 表示通过，否则返回 401 响应。"""
    if not token:
        return JSONResponse(status_code=401, content={"error": "未配置指标访问令牌"})
    auth = request.headers.get("Authorization", "")
    supplied = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
        return JSONResponse(status_code=401, content={"error": "指标访问令牌无效"})
    return None
