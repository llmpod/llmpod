"""租户概况路由。"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import current_tenant_id, require_permission
from llmpod.apps.webserver.schemas import Result, success
from llmpod.db import get_db
from llmpod.models.tenant import Tenant
from llmpod.models.user import User
from llmpod.services.wallet_service import wallet_snapshot
from llmpod.permission.permissions import TENANT_TENANT_READ

log = logging.getLogger("llmpod.apps.webserver.tenant.profile")

router = APIRouter()


class TenantInfo(BaseModel):
    id: int | None = Field(None, description="租户ID")
    name: str = Field("Unknown", description="租户名称")
    status: str = Field("unknown", description="租户状态")


class TenantProfileData(BaseModel):
    tenant: TenantInfo = Field(..., description="租户基本信息")
    user_count: int = Field(..., description="该租户下的用户数量")
    balance: Decimal = Field(Decimal("0"), description="总余额（元）")
    frozen_balance: Decimal = Field(Decimal("0"), description="在途冻结（元）")
    available: Decimal = Field(Decimal("0"), description="可用余额（元，= balance - frozen_balance）")


@router.get(
    "",
    response_model=Result[TenantProfileData],
    summary="获取租户概况",
    dependencies=[Depends(require_permission(TENANT_TENANT_READ))],
)
async def get_tenant_profile(
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    """获取当前租户概况。"""

    # 获取租户信息
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    result = await db.execute(stmt)
    tenant = result.scalar_one_or_none()

    # 统计用户数量
    count_stmt = select(func.count(User.id)).where(User.tenant_id == tenant_id)
    count_result = await db.execute(count_stmt)
    user_count = count_result.scalar() or 0

    # 余额（租户钱包，无行按 0；三段口径经 wallet_snapshot 唯一定义处）
    balance, frozen, available = await wallet_snapshot(db, tenant_id)

    return success(
        TenantProfileData(
            tenant=TenantInfo(
                id=tenant.id if tenant else None,
                name=tenant.name if tenant else "Unknown",
                status=tenant.status if tenant else "unknown",
            ),
            user_count=user_count,
            balance=balance,
            frozen_balance=frozen,
            available=available,
        )
    )
