"""钱包管理 service（管理端）：查余额 / 调额 / 流水列表 / 消费汇总。

调额走读改写（管理端低并发，与既有 service 范式一致），网关热路径的原子条件
UPDATE 见 proxyserver/billing.py。钱包行惰性创建（首次调额/充值）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.models import BalanceTransaction, Wallet


async def get_wallet(db: AsyncSession, tenant_id: int) -> Wallet | None:
    return (await db.execute(select(Wallet).where(Wallet.tenant_id == tenant_id))).scalar_one_or_none()


async def ensure_wallet(db: AsyncSession, tenant_id: int) -> Wallet:
    """惰性建行 + 行锁：SELECT FOR UPDATE 串行化与网关原子扣款的并发写。

    网关 billing 用原子 ``UPDATE wallet SET balance = balance - X``，若调额走「读旧值
    → 全行写回」会 lost update（覆盖并发扣款）。这里在读侧加行锁，把调额与扣款在 PG
    层串行化；管理端调额本就低频，持锁窗口可忽略。
    """
    wallet = (
        await db.execute(select(Wallet).where(Wallet.tenant_id == tenant_id).with_for_update())
    ).scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(tenant_id=tenant_id, balance=Decimal("0"), frozen_balance=Decimal("0"))
        db.add(wallet)
        await db.flush()
    return wallet


async def get_balance(db: AsyncSession, tenant_id: int) -> Decimal:
    wallet = await get_wallet(db, tenant_id)
    return wallet.balance if wallet is not None else Decimal("0")


async def wallet_snapshot(db: AsyncSession, tenant_id: int) -> tuple[Decimal, Decimal, Decimal]:
    """余额三段 ``(balance, frozen_balance, available)``；无钱包行按 0。

    「available = balance − frozen」与「无行按 0」的口径唯一定义处，
    管理端/租户端各查询处应经此取值而非各自重算。
    """
    wallet = await get_wallet(db, tenant_id)
    balance = wallet.balance if wallet is not None else Decimal("0")
    frozen = wallet.frozen_balance if wallet is not None else Decimal("0")
    return balance, frozen, balance - frozen


async def consume_total(db: AsyncSession, tenant_id: int, since: datetime) -> Decimal:
    """自 ``since`` 起的消费总额（consume 记负数，返回绝对值；无流水为 0）。"""
    total = (
        await db.execute(
            select(func.sum(BalanceTransaction.amount)).where(
                BalanceTransaction.tenant_id == tenant_id,
                BalanceTransaction.type == "consume",
                BalanceTransaction.created_at >= since,
            )
        )
    ).scalar()
    return abs(total) if total is not None else Decimal("0")


async def get_tenant_balance_map(db: AsyncSession, tenant_ids: list[int]) -> dict[int, tuple[Decimal, Decimal]]:
    """批量取 ``tenant_id → (balance, frozen_balance)``（租户/用户列表装配复用）。

    一条 select 拿 wallet 行（tenant_id 唯一）；无钱包行的租户由调用方按 0 兜底。
    """
    if not tenant_ids:
        return {}
    wallets = list((await db.execute(select(Wallet).where(Wallet.tenant_id.in_(tenant_ids)))).scalars())
    return {w.tenant_id: (w.balance, w.frozen_balance) for w in wallets}


async def list_wallets(db: AsyncSession) -> list[Wallet]:
    """全部钱包（含负余额租户），按余额升序（负在前，供资损排查）。"""
    rows = list((await db.execute(select(Wallet))).scalars())
    rows.sort(key=lambda w: w.balance)
    return rows


async def adjust_wallet(
    db: AsyncSession,
    *,
    tenant_id: int,
    amount: Decimal,
    mode: str,
    reason: str | None = None,
) -> Wallet:
    """调额：add（充值）/ subtract（扣减）/ override（设为目标值）；写 topup/adjust 流水。"""
    wallet = await ensure_wallet(db, tenant_id)
    old = wallet.balance
    if mode == "add":
        if amount <= 0:
            raise LLMPodValidationError("add 金额必须为正")
        new = old + amount
        delta = amount
    elif mode == "subtract":
        if amount <= 0:
            raise LLMPodValidationError("subtract 金额必须为正")
        new = old - amount
        delta = -amount
    elif mode == "override":
        if amount < 0:
            raise LLMPodValidationError("override 金额不能为负")
        new = amount
        delta = amount - old
    else:
        raise LLMPodValidationError(f"未知调额模式: {mode}")

    # 余额列是 Numeric(20,6)：入参各自合法（路由侧已按列上界校验）但相加/相减仍可越界，
    # 越界 DataError 不走 IntegrityError 处理器、会以 500 裸出——在 service 层按列宽拦成 422。
    # 负余额是合法状态（扣减/计费可透支），下界对称取负上限
    from llmpod.apps.webserver.schemas.request import NUMERIC_20_6_MAX

    if abs(new) > NUMERIC_20_6_MAX:
        raise LLMPodValidationError(f"调整后余额({new})超出列上限 ±{NUMERIC_20_6_MAX}")
    if abs(delta) > NUMERIC_20_6_MAX:
        raise LLMPodValidationError(f"流水金额({delta})超出列上限 ±{NUMERIC_20_6_MAX}")

    wallet.balance = new
    # 流水类型：充值写 topup（对账/展示语义区分于人工调额），扣减/设值写 adjust
    txn_type = "topup" if mode == "add" else "adjust"
    db.add(
        BalanceTransaction(
            tenant_id=tenant_id,
            type=txn_type,
            amount=delta,
            balance_after=new,
            meta={"reason": reason} if reason else {},
        )
    )
    await db.flush()
    return wallet


async def list_transactions(
    db: AsyncSession,
    *,
    tenant_id: int,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[BalanceTransaction], int]:
    """流水列表（按 id 倒序，SQL 分页 + 计数）。返回 (当页行, 总数)。

    流水 append-only 从不清理：全量物化再 Python 切页的成本随租户历史无界增长，
    分页下界交给数据库。
    """
    cond = BalanceTransaction.tenant_id == tenant_id
    total = (await db.execute(select(func.count()).select_from(BalanceTransaction).where(cond))).scalar() or 0
    rows = list(
        (
            await db.execute(
                select(BalanceTransaction)
                .where(cond)
                .order_by(BalanceTransaction.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
    )
    return rows, total
