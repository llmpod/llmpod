"""/v1/* 推理路由（API 网关对外契约）。

路由层只声明路径与 operation/客户端工厂；runtime 经 ``request.app.state.proxy``
取（app.py 装配时挂载），请求生命周期统一在 inference_handler 编排。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.proxyserver.inference_handler import (
    HandledError,
    authenticate_http,
    handle_inference_request,
    maintenance_reject,
    reject_response,
)
from llmpod.apps.proxyserver.protocol import (
    get_anthropic_client,
    get_openai_client,
    get_rerank_http_client,
)
from llmpod.db import get_db, get_redis
from llmpod.services.model_block_service import active_blocked_route_ids

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    return await handle_inference_request(
        request.app.state.proxy,
        request,
        db,
        redis,
        operation="chat.completions",
        get_client=get_openai_client,
    )


@router.post("/v1/messages")
async def messages(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    return await handle_inference_request(
        request.app.state.proxy,
        request,
        db,
        redis,
        operation="anthropic.messages",
        get_client=get_anthropic_client,
    )


@router.post("/v1/messages/count_tokens")
async def messages_count_tokens(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Anthropic SDK 一等公民方法（Claude Code 用它做上下文 / 自动压缩判断）。

    走完整准入（鉴权 / 授权 / 限流）但不计费不落账（adapter.records_logs=False，
    且不产生 usage）；直接透传上游计数结果。
    """
    return await handle_inference_request(
        request.app.state.proxy,
        request,
        db,
        redis,
        operation="anthropic.count_tokens",
        get_client=get_anthropic_client,
    )


@router.post("/v1/embeddings")
async def embeddings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    return await handle_inference_request(
        request.app.state.proxy,
        request,
        db,
        redis,
        operation="embeddings",
        get_client=get_openai_client,
    )


@router.post("/v1/rerank")
async def rerank(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    return await handle_inference_request(
        request.app.state.proxy,
        request,
        db,
        redis,
        operation="rerank",
        get_client=get_rerank_http_client,
    )


@router.get("/v1/models")
async def list_models(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    # 请求号盖章与推理管道同口径（OpenAI 惯例头）：鉴权/维护拒绝也要带号，
    # 客户端 SDK 启动轮询被拒时贴号可反查访问日志
    request_id = uuid.uuid4().hex
    try:
        ctx = await authenticate_http(request, db, redis)
    except HandledError as e:
        if e.reject_reason is not None:
            response = reject_response(e.meta, "models.list", e.response, e.reject_reason)
        else:
            response = e.response
        response.headers["x-request-id"] = request_id
        return response

    # 维护模式与推理管道共用 maintenance_reject 判定口径：租户 Key 拒
    # （平台直发 Key 放行），拒绝走 reject 计数、不落 request_log。
    snap = await request.app.state.proxy.get_or_build(db)
    rejected = maintenance_reject(snap.maintenance, ctx)
    if rejected is not None:
        response = reject_response(
            {"key_id": ctx.key_id, "tenant_id": ctx.tenant_id}, "models.list", rejected, "maintenance"
        )
        response.headers["x-request-id"] = request_id
        return response

    # 可见口径与 authz 一致：published 且不被本租户封禁且命中 key 的 allowed_models。
    # 列表读快照（内存，零 PG 查询）：本端点被客户端 SDK 启动时轮询，扛得住才要紧；
    # 新发布的名字最长 3s 后出现，与调用路径的回源旁路（即时）不冲突——SDK 列表
    # 刷新容忍秒级滞后，等不到下一轮的客户端可直接发起调用。封禁集保持点查：
    # 它不在快照里，且管理端封禁要求下一个请求立即生效（与 authz 同源同口径）。
    blocked_ids: set[int] = set()
    if ctx.tenant_id is not None:
        blocked_ids = await active_blocked_route_ids(db, ctx.tenant_id)

    data = [
        {"id": entry.name, "object": "model", "created": 0, "owned_by": "llmpod"}
        for entry in snap.routes.values()
        if entry.status == "published"
        and entry.route_id not in blocked_ids
        and (not ctx.allowed_models or entry.name in ctx.allowed_models)
    ]
    return {"object": "list", "data": data}


@router.get("/health")
async def health():
    return {"status": "ok", "service": "llmpod-proxyserver"}


@router.get("/metrics")
async def metrics(request: Request):
    """管道指标端点（Prometheus 文本格式；bearer token 门，fail-closed 空 token 即 401）。"""
    from prometheus_client import generate_latest

    from llmpod.config.settings import settings
    from llmpod.core.metrics_auth import verify_metrics_token

    denied = verify_metrics_token(request, settings.metrics_token)
    if denied is not None:
        return denied
    return Response(
        content=generate_latest(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
