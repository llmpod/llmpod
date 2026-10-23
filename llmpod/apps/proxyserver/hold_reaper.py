"""悬空冻结回收（hold reaper）：proxyserver 崩溃遗留的 frozen hold 自动释放。

背景：预扣（hold）落库不丢，但进程在「已预扣、未结算」窗口被 SIGKILL/宕机时，
``reset_frozen_state`` 刻意保留冻结（宁冻结勿白送）——租户可用余额会被永久锁死，
此前只能人工改库。本模块补自动对账闭环：

- 只回收 ``status=frozen`` 且 ``expires_at`` 早于 ``now - hold_reap_grace_seconds``
  的行（宽限默认 1h：长流请求的 hold 可能超 TTL 但仍在途，宽限垫底）；
- 本进程在途请求（``runtime.active_request_ids``）跳过；
- 释放走与 ``HeldReservation.release`` 同一原语（原子认领 frozen 行 + 对称解冻
  Key 预算/钱包两级），与请求路径并发安全；
- 语义取舍（写死在 docstring，勿「优化」成结算）：崩溃请求的真实用量无法从
  预扣额推断（reset_frozen_state 的既有论断），按预扣全额释放——平台最多让渡
  一次请求的费用，换取余额不永久锁死。每笔回收记 warning 留对账痕迹。

多副本 proxyserver 的已知边界：active 集不跨进程共享，他副本的在途请求理论上
可能被本副本回收——宽限 1h ≫ 正常流时长兜底；单副本是当前部署假设。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from llmpod.config.settings import settings
from llmpod.models.balance_hold import BalanceHold

log = logging.getLogger("llmpod.apps.proxyserver.hold_reaper")

# 单轮回收上限：防止大范围崩溃后（重启窗口遗留大量 frozen）单轮长事务
_BATCH_LIMIT = 100


async def reap_expired_holds(db_factory, active_request_ids: set[str]) -> int:
    """回收过期悬空冻结（单轮 ≤100 笔）；返回回收数。

    ``db_factory``：``get_db_context`` 同款异步上下文工厂（每笔一个短事务，
    避免整批长事务）。幂等：认领失败的行（并发已被结算/释放）自然跳过。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.hold_reap_grace_seconds)
    async with db_factory() as db:
        # expires_at 有索引可用：过期判定下推进 SQL（LIMIT 截批），不把全量 frozen
        # 行拉进内存再 Python 侧过滤——大范围崩溃后遗留万级行时差一个量级
        rows = list(
            (
                await db.execute(
                    select(BalanceHold)
                    .where(BalanceHold.status == "frozen", BalanceHold.expires_at < cutoff)
                    .limit(_BATCH_LIMIT)
                )
            ).scalars()
        )
    released = 0
    for hold in rows:
        if hold.request_id in active_request_ids:
            continue
        if await _release_one(db_factory, hold):
            released += 1
    if released:
        log.warning(
            "hold reaper: released %d expired frozen hold(s) (grace=%ds)",
            released,
            settings.hold_reap_grace_seconds,
        )
    return released


async def _release_one(db_factory, hold: BalanceHold) -> bool:
    """按预扣全额解冻单笔：原子认领 frozen 行 + 对称释放两级冻结。

    复用 BillingService 的原语（``_close_hold`` 认领 + ``_release_balances``
    对称解冻），保证与请求路径的资金口径单一。
    """
    from llmpod.apps.proxyserver.billing import BillingService

    svc = BillingService(db_factory)
    est = hold.est_amount or Decimal(0)
    if est <= 0:
        # 零额冻结行无资金影响，直接关单防反复扫描
        async with db_factory() as db:
            if not await svc._close_hold(db, request_id=hold.request_id, status="released", close_reason="reaper"):
                return False
            await db.commit()
        return True
    async with db_factory() as db:
        if not await svc._close_hold(db, request_id=hold.request_id, status="released", close_reason="reaper"):
            return False
        await svc._release_balances(db, tenant_id=hold.tenant_id, key_id=hold.api_key_id, est=est)
        await db.commit()
    log.warning(
        "hold reaper: released frozen hold request_id=%s tenant=%s key=%s est=%s "
        "(进程崩溃遗留，按预扣全额释放，实际用量无法推断)",
        hold.request_id,
        hold.tenant_id,
        hold.api_key_id,
        est,
    )
    return True
