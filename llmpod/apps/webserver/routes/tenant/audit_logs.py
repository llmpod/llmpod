"""租户审计日志路由（本租户作用域）。

与 admin/audit_logs 的差异：``tenant_id`` 强制 = 当前租户（``current_tenant_id``
唯一可信来源），租户侧只能查本租户相关操作（含管理员代本租户做的操作，
``actor_name`` 记录操作者）。响应形状用 ``AuditLogTenantOut``：不外露
``actor_type``（user|system 内部视角）与 ``tenant_id``（本就恒等于本租户）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import current_tenant_id, require_permission
from llmpod.apps.webserver.schemas import PaginatedResponse
from llmpod.apps.webserver.schemas.out_models import AuditLogTenantOut
from llmpod.db import get_db
from llmpod.permission.permissions import TENANT_AUDIT_LOG_READ
from llmpod.services.audit_service import list_audit_logs
from llmpod.utils.time import ms_to_datetime

router = APIRouter()


@router.get("", response_model=PaginatedResponse[AuditLogTenantOut], summary="分页查询本租户审计日志")
async def list_logs(
    _perm: None = Depends(require_permission(TENANT_AUDIT_LOG_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    actor_id: int | None = Query(None, description="按操作者ID过滤"),
    action: str | None = Query(None, description="按操作过滤：create | update | delete | login | logout"),
    resource_type: str | None = Query(None, description="按资源类型过滤：user | api_key | ..."),
    resource_id: str | None = Query(None, description="按资源ID快照过滤"),
    result: str | None = Query(None, description="按结果过滤：success | failed"),
    since_ms: int | None = Query(None, description="起始时间（毫秒时间戳，含）"),
    until_ms: int | None = Query(None, description="截止时间（毫秒时间戳，含）"),
):
    """本租户审计日志分页列表；作用域硬绑当前租户，不提供 tenant_id 搜索。"""
    rows, total = await list_audit_logs(
        db,
        page=page,
        page_size=page_size,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        since=ms_to_datetime(since_ms),
        until=ms_to_datetime(until_ms),
    )
    return PaginatedResponse.result(items=rows, total=total, page=page, page_size=page_size)
