"""租户钱包路由（余额查询 + 流水列表，只读）。

调额（充值/扣减）是平台运营动作，只存在于 admin 侧；租户端对自己的钱包
只有查看权。余额三段语义与 admin 侧一致（总余额 / 冻结 / 可用）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import current_tenant_id, require_permission
from llmpod.apps.webserver.schemas import PaginatedResponse, Result, success
from llmpod.apps.webserver.schemas.out_models import BalanceTransactionOut
from llmpod.db import get_db
from llmpod.permission.permissions import TENANT_WALLET_READ
from llmpod.services.wallet_service import list_transactions, wallet_snapshot

log = logging.getLogger("llmpod.apps.webserver.tenant.wallet")


class WalletBalanceData(BaseModel):
    """余额三段快照（无钱包行按 0 计）。金额为 Decimal，JSON 序列化为字符串。"""

    tenant_id: int
    balance: Decimal
    frozen_balance: Decimal
    available: Decimal


router = APIRouter()


@router.get("", response_model=Result[WalletBalanceData], summary="查询本租户余额")
async def get_tenant_wallet(
    _perm: None = Depends(require_permission(TENANT_WALLET_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    """本租户余额三段：总余额 / 在途冻结 / 可用。无钱包行（未充值过）按 0 计。"""
    balance, frozen, available = await wallet_snapshot(db, tenant_id)
    return success(
        WalletBalanceData(
            tenant_id=tenant_id,
            balance=balance,
            frozen_balance=frozen,
            available=available,
        )
    )


@router.get("/transactions", response_model=PaginatedResponse[BalanceTransactionOut], summary="查询本租户流水")
async def get_tenant_transactions(
    _perm: None = Depends(require_permission(TENANT_WALLET_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """本租户钱包流水分页（充值 / 调额 / 消费 / 系统），按时间倒序。"""
    rows, total = await list_transactions(db, tenant_id=tenant_id, page=page, page_size=page_size)
    return PaginatedResponse.result(items=rows, total=total, page=page, page_size=page_size)
