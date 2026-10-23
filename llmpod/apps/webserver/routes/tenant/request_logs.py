"""租户调用日志路由（本租户计量行列表 + 详情读块）。

与 admin/request_logs 同构，差异只在作用域：查询强制 ``tenant_id == 当前租户``
（跨租户的 request_id 视同不存在），只能按自己租户的 Key 过滤。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path as PathParam, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import current_tenant_id, require_permission
from llmpod.apps.webserver.schemas import PaginatedResponse, Result, success
from llmpod.apps.webserver.schemas.out_models import RequestLogDetailOut, RequestLogOut
from llmpod.db import get_db
from llmpod.models import ApiKey
from llmpod.permission.permissions import TENANT_REQUEST_LOG_READ
from llmpod.services.request_log_service import get_request_log, list_request_logs, decode_body_block, get_body_store

from llmpod.utils.time import ms_to_datetime

log = logging.getLogger("llmpod.apps.webserver.tenant.request_logs")

router = APIRouter()


async def _tenant_key_ids(db: AsyncSession, tenant_id: int, api_key_id: int | None) -> int | None:
    """Key 过滤的作用域校验：传了 api_key_id 但不属于本租户 → 返回 -1 哨兵。

    （-1 是不可能命中的 Key id，过滤结果恒空——不把「别人的 Key」误当「不过滤」；
    返回 None 才是禁用过滤。跨租户 Key 与不存在的 Key 同等对待，不泄露存在性。）"""
    if api_key_id is None:
        return None
    owned = (
        await db.execute(select(ApiKey.id).where(ApiKey.id == api_key_id, ApiKey.tenant_id == tenant_id))
    ).scalar_one_or_none()
    return api_key_id if owned is not None else -1  # -1：不存在的 Key id，结果恒空


@router.get("", response_model=PaginatedResponse[RequestLogOut], summary="查询本租户调用日志")
async def list_tenant_logs(
    _perm: None = Depends(require_permission(TENANT_REQUEST_LOG_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    api_key_id: int | None = Query(None, description="按 API Key 过滤（仅限本租户的 Key）"),
    model: str | None = Query(None, description="按对外模型名过滤"),
    status: str | None = Query(None, description="按状态过滤：success | interrupted | error"),
    conv_prefix_hash: str | None = Query(None, description="按会话指纹过滤"),
    since_ms: int | None = Query(None, description="起始时间（毫秒时间戳，含）"),
    until_ms: int | None = Query(None, description="截止时间（毫秒时间戳，含）"),
):
    """本租户计量行分页（只碰 PG 列）；Key 过滤仅接受本租户的 Key id。"""
    scoped_key_id = await _tenant_key_ids(db, tenant_id, api_key_id)
    rows, total = await list_request_logs(
        db,
        page=page,
        page_size=page_size,
        tenant_id=tenant_id,
        api_key_id=scoped_key_id,
        model=model,
        status=status,
        conv_prefix_hash=conv_prefix_hash,
        since=ms_to_datetime(since_ms),
        until=ms_to_datetime(until_ms),
    )
    return PaginatedResponse.result(items=rows, total=total, page=page, page_size=page_size)


@router.get("/{request_id}", response_model=Result[RequestLogDetailOut], summary="本租户调用日志详情")
async def tenant_log_detail(
    request_id: Annotated[str, PathParam()],
    _perm: None = Depends(require_permission(TENANT_REQUEST_LOG_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    """单条调用详情（仅本租户）：计量行 + BodyStore 还原的请求/响应体。"""
    row = await get_request_log(db, request_id=request_id, tenant_id=tenant_id)
    store = get_body_store()
    out = RequestLogDetailOut.model_validate(row)
    out.request_body = decode_body_block(store, row.request_ref)
    out.response_body = decode_body_block(store, row.response_ref)
    return success(out)
