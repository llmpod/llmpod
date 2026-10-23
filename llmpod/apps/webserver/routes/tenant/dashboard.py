"""租户端数据看板路由（控制台整屏聚合）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import current_tenant_id, require_permission
from llmpod.apps.webserver.schemas import Result, success
from llmpod.db import get_db
from llmpod.permission.permissions import TENANT_DASHBOARD_READ
from llmpod.services import tenant_dashboard_service as svc

router = APIRouter()


class TenantTrendPointOut(BaseModel):
    """趋势单点（近 N 天逐日）。"""

    date: str = Field(..., description="日期（ISO8601，本地时区）")
    call_count: int = Field(..., description="当日调用量（触达上游）")
    error_rate: float = Field(..., description="当日错误率 0-1")


class TenantModelBriefOut(BaseModel):
    """可用模型简行。"""

    id: int = Field(..., description="路由ID")
    name: str = Field(..., description="模型名（对外别名）")


class TenantRecentCallOut(BaseModel):
    """最近调用行。"""

    model: str | None = Field(None, description="模型名（对外别名）")
    input_tokens: int = Field(..., description="输入 token")
    output_tokens: int = Field(..., description="输出 token")
    latency_ms: int | None = Field(None, description="端到端延迟（毫秒）")
    status: str = Field(..., description="结果：success | error | interrupted")
    created_at: str | None = Field(None, description="时间（ISO8601）")


class TenantScreenOut(BaseModel):
    """租户控制台整屏聚合（单请求）。"""

    calls: int = Field(..., description="窗口内调用次数")
    active_models: int = Field(..., description="窗口内有流量的模型数")
    error_rate: float = Field(..., description="窗口内错误率 0-1")
    total_tokens: int = Field(..., description="窗口内总 token")
    avg_ttft_ms: int | None = Field(None, description="平均首帧延迟（毫秒，仅流式）")
    trend: list[TenantTrendPointOut] = Field(..., description="近 7 日调用趋势（空日补 0）")
    models: list[TenantModelBriefOut] = Field(..., description="本租户可用模型（published − 封禁）")
    recent_calls: list[TenantRecentCallOut] = Field(..., description="最近调用（前 5 条）")
    balance: Decimal = Field(..., description="总余额（元）")
    frozen_balance: Decimal = Field(..., description="在途冻结（元）")
    available: Decimal = Field(..., description="可用余额（元）")
    month_consume: Decimal = Field(..., description="本月消费（元，consume 汇总）")


@router.get("/screen", response_model=Result[TenantScreenOut], summary="租户控制台整屏聚合（单请求）")
async def tenant_dashboard_screen(
    _perm: None = Depends(require_permission(TENANT_DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
    from_: Annotated[
        datetime | None, Query(alias="from", description="起始时间 ISO8601；缺省今日 00:00（本地）")
    ] = None,
    to: Annotated[datetime | None, Query(description="结束时间 ISO8601；缺省当前时间")] = None,
):
    """统计卡（窗口内，本租户）+ 近 7 日趋势 + 可用模型 + 最近调用 + 余额/月消费。"""
    data = await svc.tenant_screen(db, tenant_id, from_=from_, to=to)
    return success(TenantScreenOut(**data))
