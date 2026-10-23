"""管理员调用日志路由（计量行列表 + 详情读块）。

列表只碰 PG 计量列（索引/筛选）；详情才读 BodyStore 块还原 body。
查询逻辑在 request_log_service（tenant 路由复用，admin 侧不传 tenant_id = 只看平台记录）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path as PathParam, Query
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import require_permission
from llmpod.apps.webserver.schemas import PaginatedResponse, Result, success
from llmpod.apps.webserver.schemas.out_models import RequestLogAdminDetailOut, RequestLogAdminOut
from llmpod.services.tenant_scope import platform_scope_requested
from llmpod.db import get_db
from llmpod.permission.permissions import REQUEST_LOG_READ
from llmpod.services.request_log_service import get_request_log, list_request_logs, decode_body_block, get_body_store

from llmpod.utils.time import ms_to_datetime

log = logging.getLogger("llmpod.apps.webserver.admin.request_logs")

router = APIRouter()


@router.get(
    "", response_model=PaginatedResponse[RequestLogAdminOut], summary="分页查询调用日志（平台域，默认不含租户记录）"
)
async def list_logs(
    _perm: None = Depends(require_permission(REQUEST_LOG_READ)),
    db: AsyncSession = Depends(get_db),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    tenant_id: int | None = Query(None, description="按租户ID过滤（不传或传 0=平台记录，不含租户记录）"),
    api_key_id: int | None = Query(None, description="按 API Key 过滤"),
    model: str | None = Query(None, description="按对外模型名过滤"),
    status: str | None = Query(None, description="按状态过滤：success | interrupted | error"),
    conv_prefix_hash: str | None = Query(None, description="按会话指纹过滤（同会话连续请求）"),
    since_ms: int | None = Query(None, description="起始时间（毫秒时间戳，含）"),
    until_ms: int | None = Query(None, description="截止时间（毫秒时间戳，含）"),
):
    """计量行分页列表（只碰 PG 列，不读 body 块）；筛选支持租户/Key/模型/状态/会话指纹/时间区间。

    默认只展示平台记录（平台 Key 计量行，``tenant_id`` 为空）；要看某个租户的计量记录
    须显式传 ``tenant_id`` 过滤。
    """
    rows, total = await list_request_logs(
        db,
        page=page,
        page_size=page_size,
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        model=model,
        status=status,
        conv_prefix_hash=conv_prefix_hash,
        since=ms_to_datetime(since_ms),
        until=ms_to_datetime(until_ms),
        exclude_tenant_scope=platform_scope_requested(tenant_id, exclude_tenant_scope=True),
    )
    return PaginatedResponse.result(items=rows, total=total, page=page, page_size=page_size)


@router.get("/{request_id}", response_model=Result[RequestLogAdminDetailOut], summary="调用日志详情")
async def log_detail(
    request_id: Annotated[str, PathParam()],
    _perm: None = Depends(require_permission(REQUEST_LOG_READ)),
    db: AsyncSession = Depends(get_db),
):
    """单条调用详情：计量行 + 从 BodyStore 读块还原的完整请求/响应体。

    ``snapshot_disabled=true``（当时块写失败）或块已被 GC 时 body 为 null。
    """
    row = await get_request_log(db, request_id=request_id)
    store = get_body_store()
    out = RequestLogAdminDetailOut.model_validate(row)
    out.request_body = decode_body_block(store, row.request_ref)
    out.response_body = decode_body_block(store, row.response_ref)
    return success(out)
