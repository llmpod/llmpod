"""管理员审计日志路由（平台全量视角）。

列表只碰 PG 审计行（索引/筛选），查询逻辑在 audit_service（tenant 路由复用，
admin 侧为平台全量视角，tenant_id 为可选筛选而非强制作用域）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import require_permission
from llmpod.apps.webserver.schemas import PaginatedResponse
from llmpod.apps.webserver.schemas.out_models import AuditLogOut
from llmpod.db import get_db
from llmpod.permission.permissions import AUDIT_LOG_READ
from llmpod.services.tenant_scope import platform_scope_requested
from llmpod.services.audit_service import list_audit_logs
from llmpod.utils.time import ms_to_datetime

router = APIRouter()


@router.get("", response_model=PaginatedResponse[AuditLogOut], summary="分页查询审计日志（平台域，默认不含租户操作）")
async def list_logs(
    _perm: None = Depends(require_permission(AUDIT_LOG_READ)),
    db: AsyncSession = Depends(get_db),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    tenant_id: int | None = Query(
        None, description="按数据归属租户ID过滤（不传或传 0 = 平台域：无归属操作 + 平台租户操作；N≥1 = 该租户）"
    ),
    actor_id: int | None = Query(None, description="按操作者ID过滤"),
    action: str | None = Query(None, description="按操作过滤：create | update | delete | login | logout"),
    resource_type: str | None = Query(None, description="按资源类型过滤：user | tenant | api_key | ..."),
    resource_id: str | None = Query(None, description="按资源ID快照过滤"),
    result: str | None = Query(None, description="按结果过滤：success | failed"),
    since_ms: int | None = Query(None, description="起始时间（毫秒时间戳，含）"),
    until_ms: int | None = Query(None, description="截止时间（毫秒时间戳，含）"),
):
    """审计日志分页列表（平台域视角）。

    默认展示整个平台域：无租户维度的操作（节点/集群/用户/租户管理等）与平台租户自身
    的操作（平台用户/角色/登录）都在内；要看某个租户的操作记录须显式传 ``tenant_id``。
    传 ``tenant_id=0`` 与不传等价（同为平台域）。
    """
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
        exclude_tenant_scope=platform_scope_requested(tenant_id, exclude_tenant_scope=True),
    )
    return PaginatedResponse.result(items=rows, total=total, page=page, page_size=page_size)
