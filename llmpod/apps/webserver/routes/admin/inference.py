"""管理端「在线调试」转发接口：把 AI 调用透传到网关 proxyserver。

管理员用管理端登录态即可直接跑模型 API，无需选 API Key：服务端按保留名确保存在一条
active 平台 Key（tenant_id=None，缺失自动创建、已禁用自动启用）注入转发请求，
proxyserver 走「平台直发」语义（跳过租户封禁与钱包扣费）。请求/响应按字节透传，
SSE 流式原样回传，本层不解析协议体。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from llmpod.config.settings import settings
from llmpod.db import get_db
from llmpod.permission.permissions import INFERENCE_DEBUG
from llmpod.apps.webserver.auth.deps import require_permission
from llmpod.services.api_key_service import ensure_active_platform_key

log = logging.getLogger("llmpod.apps.webserver.admin.inference")

router = APIRouter()

# 平台转发 Key 的保留显示名：转发时按名点查，缺失自动创建、已禁用自动重新启用。
_PLATFORM_PROXY_KEY_NAME = "control-plane-proxy"

# 转发时丢弃的逐跳响应头（长度/连接由传输层负责，不能透传给客户端）
_HOP_BY_HOP_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive", "proxy-authenticate"}
)


def _proxy_base() -> str:
    """网关内部基址：``LLMPOD_PROXY_INTERNAL_URL`` 优先，未配则回环 + proxy_port。

    容器部署下 webserver 与 proxyserver 分容器，回环够不到对方，compose 把该值指到
    服务名（http://proxyserver:<proxy_port>）；dev 两进程同宿主时留空即可。
    取值先 strip：空白残串也算未配，避免拼出带空格的假地址。
    """
    base = settings.proxy_internal_url.strip().rstrip("/")
    return base or f"http://127.0.0.1:{settings.proxy_port}"


async def _close_client(client: httpx.AsyncClient, upstream: httpx.Response) -> None:
    """流式结束后关闭上游响应与客户端（BackgroundTask 执行）。"""
    await upstream.aclose()
    await client.aclose()


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def forward_inference(
    request: Request,
    path: str,
    _perm: None = Depends(require_permission(INFERENCE_DEBUG)),
    db: AsyncSession = Depends(get_db),
):
    """透传 AI 调用到 proxyserver：注入平台 Key，字节级回传（含 SSE 流式）。"""
    body = await request.body()
    key = await ensure_active_platform_key(db, name=_PLATFORM_PROXY_KEY_NAME)
    await db.commit()
    url = f"{_proxy_base()}/v1/{path}"

    headers = {"Authorization": f"Bearer {key.plaintext}"}
    if body:
        headers["Content-Type"] = request.headers.get("content-type", "application/json")

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0))
    try:
        req = client.build_request(request.method, url, content=body, headers=headers)
        upstream = await client.send(req, stream=True)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        # InvalidURL（配置的基址端口非法等）不是 HTTPError 子类，一并收进 502，
        # 且 aclose() 必须执行——跳过会泄漏客户端连接
        await client.aclose()
        log.warning("inference proxy forward failed (path=%s): %r", path, e)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "API gateway unreachable", "type": "api_error", "code": "bad_gateway"}},
        )

    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS and k.lower() != "content-type"
    }
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
        background=BackgroundTask(_close_client, client, upstream),
    )
