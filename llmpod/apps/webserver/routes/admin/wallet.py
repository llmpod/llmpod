"""管理员钱包路由（余额查询 / 调额 / 流水列表）。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import PaginatedResponse, Result, success
from llmpod.apps.webserver.schemas.request import NUMERIC_20_6_MAX
from llmpod.apps.webserver.schemas.out_models import BalanceTransactionOut, WalletOut
from llmpod.db import get_db
from llmpod.permission.permissions import WALLET_READ, WALLET_UPDATE
from llmpod.services.audit_service import audit_action
from llmpod.services.tenant_service import get_tenant
from llmpod.services.wallet_service import adjust_wallet, list_transactions, list_wallets, wallet_snapshot

log = logging.getLogger("llmpod.apps.webserver.admin.wallet")


class WalletBalanceData(BaseModel):
    """余额三段快照（无钱包行按 0 计）。金额为 Decimal，JSON 序列化为字符串。"""

    tenant_id: int
    balance: Decimal
    frozen_balance: Decimal
    available: Decimal


router = APIRouter()
wallets_router = APIRouter()  # 全局钱包列表（跨租户，供负余额排查）


class AdjustRequest(BaseModel):
    amount: Decimal = Field(
        ...,
        le=NUMERIC_20_6_MAX,
        description="金额（add/subtract 为正数；override 为目标值）；上界 = wallet.balance / "
        "balance_transaction.amount 两列 Numeric(20,6) 的可容纳范围",
    )
    mode: str = Field(..., pattern="^(add|subtract|override)$", description="add | subtract | override")
    reason: str | None = Field(None, description="调额原因（写入流水 meta）")


@router.get("", response_model=Result[WalletBalanceData], summary="查询租户余额")
async def get_wallet_balance(
    tenant_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(WALLET_READ)),
    db: AsyncSession = Depends(get_db),
):
    """余额查询；无钱包行（未充值过）按 0 计。返回总余额 / 冻结 / 可用。"""
    await get_tenant(db, tenant_id)
    balance, frozen, available = await wallet_snapshot(db, tenant_id)
    return success(
        WalletBalanceData(
            tenant_id=tenant_id,
            balance=balance,
            frozen_balance=frozen,
            available=available,
        )
    )


@router.get("/transactions", response_model=PaginatedResponse[BalanceTransactionOut], summary="查询租户流水")
async def get_transactions(
    tenant_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(WALLET_READ)),
    db: AsyncSession = Depends(get_db),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """钱包流水分页（充值 / 调额 / 消费 / 系统），按时间倒序；余额轨迹对账用。"""
    await get_tenant(db, tenant_id)
    rows, total = await list_transactions(db, tenant_id=tenant_id, page=page, page_size=page_size)
    return PaginatedResponse.result(items=rows, total=total, page=page, page_size=page_size)


@router.post("/adjust", response_model=Result[WalletOut], summary="调整租户余额")
@audit_action(action="update", resource="wallet", resource_id="tenant_id", tenant_id_arg="tenant_id")
async def post_adjust(
    tenant_id: Annotated[int, Path()],
    body: AdjustRequest,
    _perm: None = Depends(require_permission(WALLET_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """人工调额（add/subtract/override），写 topup/adjust 流水（amount 为 0 时降级 system）。

    用原子 UPDATE 落库（不读改写），与网关扣费并发安全。
    """
    await get_tenant(db, tenant_id)
    wallet = await adjust_wallet(db, tenant_id=tenant_id, amount=body.amount, mode=body.mode, reason=body.reason)
    await db.commit()
    return success(WalletOut.model_validate(wallet))


@wallets_router.get("", response_model=Result[list[WalletOut]], summary="列出全部租户钱包")
async def list_all_wallets(
    _perm: None = Depends(require_permission(WALLET_READ)),
    db: AsyncSession = Depends(get_db),
):
    wallets = await list_wallets(db)
    return success([WalletOut.model_validate(w) for w in wallets])
