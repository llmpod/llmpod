"""API 网关钱包扣费服务：冻结预扣（freeze）+ 结算（settle）+ 解冻（release）。

PG 独占记账：每个操作一个独立短事务（db_factory 开短 session），原子条件 UPDATE +
流水 INSERT 同事务 commit。hold 只冻 frozen_balance（balance 不动），settle 真正扣
balance 并释放冻结，release 只释放冻结不写流水。冻结生命周期落 balance_hold
（frozen → settled/released）：进程崩溃遗留的悬空 frozen 由 hold_reaper 周期回收
（见 hold_reaper.py），启动期对账见 :func:`reset_frozen_state`。平台 Key
（tenant_id 为空 = 管理端直发）不触钱包：不冻钱包、不扣 balance、不写流水；但
budget_limit 照样拦截——与租户 Key 走同一条 Key 预算条件冻结原语（budget_frozen
原子占位挡并发超扣），未设上限（NULL）= 不限、永远放行。真实消费累计进
api_key.budget_spent 供管理端展示。SQL 原语收敛在私有方法里，测试以子类桩替换。
"""

from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select, update

from llmpod.apps.proxyserver.authn import ApiKeyContext
from llmpod.apps.proxyserver.snapshot import RouteEntry
from llmpod.config.settings import settings
from llmpod.models import ApiKey, BalanceHold, BalanceTransaction, Wallet

log = logging.getLogger("llmpod.proxyserver.billing")


class InsufficientBalance(Exception):
    """钱包可用余额不足（路由层转 402）。"""


class KeyBudgetExceeded(Exception):
    """Key 预算超限（路由层转 402）。"""


class HeldReservation:
    """一次预扣的 guard：结算显式闭环，未结算即释放（解冻）。

    hold 返回本对象（租户 Key 零成本预扣返回 None）；调用方语义：
    ``held = await billing.hold(...)`` → 转发成功后 ``await held.settle(actual)``
    （或经 inference_handler 的 _settle_billing）→ 任何未产出响应的异常路径由
    ``held.release()`` 兜底解冻。资金不变量从「各出口记得调退款」收敛为
    「guard 未闭环必释放」。

    平台 Key 也持本对象：两级冻结里只参与 Key 预算这一级（无钱包冻结、
    无 balance_hold 行），settle/release/close 生命周期与租户 Key 完全一致——
    inference_handler 侧只维持一条 guard 生命周期。
    """

    def __init__(self, service: "BillingService", ctx: ApiKeyContext, entry: RouteEntry, est_cost: Decimal) -> None:
        self._lock = asyncio.Lock()
        self._service = service
        self._ctx = ctx
        self._entry = entry
        self.est_cost = est_cost
        self._settled = False
        self._streaming = False
        self._released = False
        self._settle_failed = False

    async def settle(
        self, actual_cost: Decimal | None, *, request_id: str | None = None, meta: dict | None = None
    ) -> None:
        """结算（多退少补）；幂等：已结算重复调用静默跳过。

        ``_settled`` 在服务调用成功后才置位。结算失败时保留冻结并标记失败，
        后续 release/close 不得把已交付的内容免费解冻；启动恢复也保留未结账。
        """
        async with self._lock:
            if self._settled or self._released:
                return
            try:
                await self._service.settle(
                    self._ctx, self._entry, actual_cost, self.est_cost, request_id=request_id, meta=meta
                )
            except BaseException:
                # 已产出内容不可按上游失败解冻；保留冻结供恢复/人工对账。
                self._settle_failed = True
                raise
            self._settled = True

    def mark_streaming(self) -> None:
        """流式响应已交付：结算责任移交流式生成器，release() 不再提前全额解冻。

        防止外层 finally 在生成器被消费前提前全额解冻、再由生成器 finally 结算，
        造成双重调账。``_streaming`` 只挡 release（防双调账），不挡 close——
        生成器被丢弃未消费（客户端断连/中间件丢响应）时其 try/finally 不会执行，
        此时结算/解冻都没发生，必须由 close() 兜底解冻，否则冻结余额悬空到重启。
        """
        self._streaming = True

    async def release(self, *, request_id: str | None = None, meta: dict | None = None) -> None:
        """全额解冻（未结算时）；已结算 / 已解冻 / 已交付流式 / 未预扣为 no-op。

        ``_released`` 置位保证幂等；结算失败后的冻结必须人工核对，禁止解冻。
        """
        async with self._lock:
            if self._settled or self._released or self._streaming or self._settle_failed or self.est_cost <= 0:
                return
            await self._service.release(self._ctx, self._entry, self.est_cost, request_id=request_id, meta=meta)
            self._released = True

    async def close(self, *, request_id: str | None = None, meta: dict | None = None) -> None:
        """资金闭环终点（外层 finally 必调）：结算了就 nothing，没结算就解冻。

        与 release 的唯一区别是 ``_streaming`` 不短路——流式响应对象返回后生成器
        可能永远不执行（丢弃/断连/进程重启前的窗口），close 保证这条路径上冻结
        余额必被释放；生成器若正常执行，settle 已闭环则 no-op，结算失败则
        保留冻结等待核对。幂等。
        """
        async with self._lock:
            if self._settled or self._released or self._settle_failed or self.est_cost <= 0:
                return
            # close 只在「流被丢弃/客户端断连」语境下真正解冻，关单原因记 client_abort
            await self._service.release(
                self._ctx, self._entry, self.est_cost, request_id=request_id, meta=meta, close_reason="client_abort"
            )
            self._released = True


class BillingService:
    """钱包扣费编排：冻结/结算/解冻 + 负余额告警。SQL 原子原语见私有方法。"""

    def __init__(self, db_factory, lease_ttl_seconds: int | None = None) -> None:
        self._db_factory = db_factory
        self._lease_ttl_seconds = (
            lease_ttl_seconds if lease_ttl_seconds is not None else settings.fixed_lease_ttl_seconds
        )

    def _expiry(self) -> datetime:
        """冻结租约时间：写入 balance_hold.expires_at，hold_reaper 以它判过期（见该模块）。"""
        return datetime.now(timezone.utc) + timedelta(seconds=self._lease_ttl_seconds)

    async def hold(
        self, ctx: ApiKeyContext, entry: RouteEntry, est_cost: Decimal, *, request_id: str
    ) -> HeldReservation | None:
        """预扣（冻结）：Key 预算 + 钱包两级原子冻结；零成本跳过（返回 None）。

        只动 frozen 列（budget_frozen / frozen_balance），balance 不动；同事务插一条
        balance_hold frozen 行。不足抛异常。返回 guard：结算走 ``held.settle(actual)``，
        任何未结算路径调 ``held.release()`` 解冻。

        平台 Key（tenant_id 为空）只走 Key 预算这一级：鉴权快照里 budget_limit 为空
        （不限）或估算零成本则不进 DB——无上限的 Key 预扣级零 DB 写（每请求一次
        api_key 行锁 UPDATE 纯属浪费）；上限快照可能滞后（DEL 失效协议 + TTL 兜底，
        与 rpm/status 同口径），进了闸门后行上的 budget_limit 才是判定的真值。不动
        钱包、不写 balance_hold 行。**恒返回 guard**：消费由 ``_settle_billing``
        经 guard 驱动，返回 None 则该次调用永远不会被记账。
        """
        if ctx.tenant_id is None:
            if ctx.platform_budget_gated and est_cost > 0:
                async with self._db_factory() as db:
                    await self._freeze_key_budget(db, key_id=ctx.key_id, amount=est_cost)
                    await db.commit()
            return HeldReservation(self, ctx, entry, est_cost)
        if est_cost <= 0:
            return None
        async with self._db_factory() as db:
            await self._freeze(db, key_id=ctx.key_id, tenant_id=ctx.tenant_id, amount=est_cost)
            await self._insert_hold(
                db,
                tenant_id=ctx.tenant_id,
                key_id=ctx.key_id,
                route_id=entry.route_id,
                request_id=request_id,
                est_amount=est_cost,
                expires_at=self._expiry(),
            )
            await db.commit()
        return HeldReservation(self, ctx, entry, est_cost)

    async def settle(
        self,
        ctx: ApiKeyContext,
        entry: RouteEntry,
        actual_cost: Decimal | None,
        est_cost: Decimal,
        *,
        request_id: str | None = None,
        meta: dict | None = None,
    ) -> None:
        """结算：真实费用真正扣 balance、释放冻结；写 consume 流水；负余额打告警。

        actual_cost 为 None（引擎未上报 usage / 无法计价）按 0 元处理——balance 不动、
        frozen 全释放，资金不悬空。宁可少收（计量行仍有 token 数可事后对账）也不
        静默吞掉预扣。

        平台 Key 走 :meth:`_settle_platform`（记消费 + 释放 Key 预算占位，不触钱包）。
        """
        actual = actual_cost if actual_cost is not None else Decimal(0)
        if ctx.tenant_id is None:
            await self._settle_platform(ctx, actual=actual, est=est_cost, request_id=request_id)
            return
        async with self._db_factory() as db:
            # 先在同一事务里认领冻结单，再扣款；重复/并发结算只有一个能认领。
            if not await self._close_hold(db, request_id=request_id, status="settled"):
                return
            balance_after = await self._settle_balances(
                db, tenant_id=ctx.tenant_id, key_id=ctx.key_id, actual=actual, est=est_cost
            )
            await self._insert_ledger(
                db,
                txn_type="consume",
                tenant_id=ctx.tenant_id,
                key_id=ctx.key_id,
                key_name=ctx.key_name,
                route_id=entry.route_id,
                route_name=entry.name,
                request_id=request_id,
                amount=-actual,
                balance_after=balance_after,
                meta=meta,
            )
            await db.commit()
        if balance_after < 0:
            log.error(
                "negative balance (tenant=%s route=%s amount=%s after=%s)",
                ctx.tenant_id,
                entry.name,
                actual,
                balance_after,
            )

    async def _settle_platform(
        self, ctx: ApiKeyContext, *, actual: Decimal, est: Decimal, request_id: str | None
    ) -> None:
        """平台 Key 结算：budget_spent += actual、budget_frozen 释放 est（夹非负）。
        不触钱包、不写流水（本就无钱包冻结、无 balance_hold 行）。

        消费照样入账（负值调减与租户路径同口径）；actual 为 0 但 est > 0 时仍要落库
        ——预扣的冻结占位只能在这里释放，跳过会悬空到重启。无上限 Key（快照闸门
        未开）本就没冻结过，两者皆零或无冻结时免空写（0 值 UPDATE 只是白拿行锁）。
        无请求级持久回执：提交结果不明时保留冻结并记录 request_id 供对账，
        不自动重试，以免重复累计消费。
        """
        frozen = ctx.platform_budget_gated and est > 0
        if not actual and not frozen:
            return
        try:
            async with self._db_factory() as db:
                r = await self._settle_key_budget(
                    db, key_id=ctx.key_id, amount=actual, est=est if frozen else Decimal(0)
                )
                if r.rowcount != 1:
                    raise RuntimeError(f"platform key row missing during settlement: key={ctx.key_id}")
                await db.commit()
        except Exception:
            log.error(
                "platform key spend dropped (key=%s amount=%s request_id=%s) —— "
                "按 request_log 同 request_id 的 cost 可复算补齐",
                ctx.key_id,
                actual,
                request_id,
            )
            raise

    async def release(
        self,
        ctx: ApiKeyContext,
        entry: RouteEntry,
        est_cost: Decimal,
        *,
        request_id: str | None = None,
        meta: dict | None = None,
        close_reason: str = "upstream_error",
    ) -> None:
        """解冻（下游失败）：只释放冻结、不动 balance、不写流水；hold 置 released。

        ``close_reason`` 按实际失败来源传（列 comment 口径：upstream_error |
        client_abort | expired | startup）；流式丢弃走 HeldReservation.close 的
        client_abort。平台 Key 无钱包冻结，只释放 Key 预算占位（budget_frozen）；
        快照闸门未开的平台 Key 预扣本就没冻结过（hold 跳过），解冻同样免进 DB。
        """
        if est_cost <= 0:
            return
        if ctx.tenant_id is None and not ctx.platform_budget_gated:
            return
        if ctx.tenant_id is None:
            async with self._db_factory() as db:
                await self._release_key_budget(db, key_id=ctx.key_id, est=est_cost)
                await db.commit()
            return
        async with self._db_factory() as db:
            if not await self._close_hold(db, request_id=request_id, status="released", close_reason=close_reason):
                return
            await self._release_balances(db, tenant_id=ctx.tenant_id, key_id=ctx.key_id, est=est_cost)
            await db.commit()

    # —— 原子 SQL 原语（测试以子类桩替换）——
    # Key 预算级原语（_freeze_key_budget / _settle_key_budget / _release_key_budget）
    # 是资金闸门的唯一谓词，租户路径的组合原语（_freeze / _settle_balances /
    # _release_balances）内部复用它们再叠加钱包一级——两级语义永远同源，改谓词只改一处。

    async def _freeze_key_budget(self, db, *, key_id: int, amount: Decimal) -> None:
        """Key 预算条件冻结：NULL 上限放行，spent + frozen + amount 超限拒（零行）。
        原子 UPDATE 占位挡并发超扣。"""
        r = await db.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .where(
                or_(
                    ApiKey.budget_limit.is_(None),
                    ApiKey.budget_spent + ApiKey.budget_frozen + amount <= ApiKey.budget_limit,
                )
            )
            .values(budget_frozen=ApiKey.budget_frozen + amount)
        )
        if r.rowcount == 0:
            raise KeyBudgetExceeded("key budget exceeded")

    async def _settle_key_budget(self, db, *, key_id: int, amount: Decimal, est: Decimal):
        """Key 预算结算：budget_spent += amount、budget_frozen 释放 est（夹非负）。

        返回结果带 rowcount（Key 行被删时为 0，调用方据此留对账日志）。
        """
        return await db.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(
                budget_spent=ApiKey.budget_spent + amount,
                budget_frozen=func.greatest(ApiKey.budget_frozen - est, 0),
            )
        )

    async def _release_key_budget(self, db, *, key_id: int, est: Decimal) -> None:
        """Key 预算占位释放：budget_frozen -= est（夹非负）。"""
        await db.execute(
            update(ApiKey).where(ApiKey.id == key_id).values(budget_frozen=func.greatest(ApiKey.budget_frozen - est, 0))
        )

    async def _freeze(self, db, *, key_id: int, tenant_id: int, amount: Decimal) -> None:
        """同事务两级原子冻结：Key 预算条件冻结 + 钱包可用余额条件冻结；任一不足抛异常回滚。

        锁序先 ApiKey 后 Wallet（settle/release 同序），反向加锁会 AB-BA 死锁（40P01）。
        """
        await self._freeze_key_budget(db, key_id=key_id, amount=amount)
        r = await db.execute(
            update(Wallet)
            .where(Wallet.tenant_id == tenant_id, Wallet.balance - Wallet.frozen_balance >= amount)
            .values(frozen_balance=Wallet.frozen_balance + amount)
        )
        if r.rowcount == 0:
            raise InsufficientBalance("insufficient balance")

    async def _settle_balances(self, db, *, tenant_id: int, key_id: int, actual: Decimal, est: Decimal) -> Decimal:
        """结算：Key 预算 budget_spent += actual、budget_frozen -= est（夹非负）；
        钱包 balance -= actual、frozen -= est（夹非负）。返回扣后总余额。

        锁序与 ``_freeze`` 一致（先 ApiKey 后 Wallet）——hold 与 settle 并发时若反向加锁
        会 AB-BA 死锁（40P01）。
        """
        key_result = await self._settle_key_budget(db, key_id=key_id, amount=actual, est=est)
        if key_result.rowcount != 1:
            raise RuntimeError(f"tenant key row missing during settlement: key={key_id}")
        r = await db.execute(
            update(Wallet)
            .where(Wallet.tenant_id == tenant_id)
            .values(
                balance=Wallet.balance - actual,
                frozen_balance=func.greatest(Wallet.frozen_balance - est, 0),
            )
            .returning(Wallet.balance)
        )
        return r.scalar_one()

    async def _release_balances(self, db, *, tenant_id: int, key_id: int, est: Decimal) -> Decimal:
        """解冻：Key 预算 budget_frozen -= est、钱包 frozen -= est（均夹非负）；balance 不动。

        锁序与 ``_freeze`` 一致（先 ApiKey 后 Wallet），避免 hold 与 release 并发时 AB-BA 死锁。
        """
        await self._release_key_budget(db, key_id=key_id, est=est)
        r = await db.execute(
            update(Wallet)
            .where(Wallet.tenant_id == tenant_id)
            .values(frozen_balance=func.greatest(Wallet.frozen_balance - est, 0))
            .returning(Wallet.balance)
        )
        return r.scalar_one()

    async def _insert_hold(
        self,
        db,
        *,
        tenant_id: int,
        key_id: int,
        route_id: int,
        request_id: str,
        est_amount: Decimal,
        expires_at: datetime,
    ) -> None:
        """插一条 frozen 冻结行：expires_at 记租约到期，hold_reaper 以它判过期回收。"""
        db.add(
            BalanceHold(
                tenant_id=tenant_id,
                api_key_id=key_id,
                route_id=route_id,
                request_id=request_id,
                est_amount=est_amount,
                status="frozen",
                expires_at=expires_at,
            )
        )
        await db.flush()

    async def _close_hold(self, db, *, request_id: str | None, status: str, close_reason: str | None = None) -> bool:
        """原子认领 frozen 冻结行；只有认领成功的事务可扣费或解冻。"""
        if request_id is None:
            raise ValueError("tenant settlement requires request_id")
        result = await db.execute(
            update(BalanceHold)
            .where(BalanceHold.request_id == request_id, BalanceHold.status == "frozen")
            .values(status=status, close_reason=close_reason, closed_at=func.now())
        )
        if result.rowcount == 1:
            return True
        existing = await db.scalar(select(BalanceHold.status).where(BalanceHold.request_id == request_id))
        if existing is None:
            raise RuntimeError(f"tenant reservation missing: request_id={request_id}")
        return False

    async def _insert_ledger(
        self,
        db,
        *,
        txn_type: str,
        tenant_id: int,
        key_id: int,
        key_name: str,
        route_id: int,
        route_name: str,
        request_id: str | None,
        amount: Decimal,
        balance_after: Decimal,
        meta: dict | None,
    ) -> None:
        row = BalanceTransaction(
            tenant_id=tenant_id,
            api_key_id=key_id,
            api_key_name=key_name,
            route_id=route_id,
            route_name=route_name,
            request_id=request_id,
            type=txn_type,
            amount=amount,
            balance_after=balance_after,
            meta=meta or {},
        )
        db.add(row)
        await db.flush()


async def reset_frozen_state(db) -> None:
    """仅在没有未结冻结时归零；有待结资金则保留给人工对账。

    已交付的请求可能因 PG 结算失败仍处于 frozen。启动时盲目释放会把内容
    免费送出；未结算的实际费用无法从预扣额推断，必须保留账本和冻结状态。

    钱包侧 frozen hold 有 ``balance_hold`` 明细行，悬空冻结由 hold_reaper 周期
    回收（按预扣全额释放，平台最多让渡一次请求费用）；平台 Key 的 ``budget_frozen``
    只有计数无明细（不写 balance_hold，平台 Key 永不碰钱包），没有可对账的行，
    reaper 扫不到——单副本部署下重启即代表旧进程的预扣必无人结算，这里直接
    归零释放，让渡同口径、预算不被永久锁死。
    """
    holds = list(
        (await db.execute(select(BalanceHold.request_id).where(BalanceHold.status == "frozen").limit(5))).scalars()
    )
    wallet_frozen = any(v and v > 0 for v in (await db.execute(select(Wallet.frozen_balance))).scalars())
    if holds or wallet_frozen:
        log.critical(
            "unsettled billing reservations preserved at startup; reconcile before clearing freezes (sample request_ids=%s)",
            holds,
        )
        return
    now = datetime.now(timezone.utc)
    key_frozen_ids = list((await db.execute(select(ApiKey.id).where(ApiKey.budget_frozen > 0).limit(5))).scalars())
    if key_frozen_ids:
        log.warning(
            "platform key budget_frozen cleared at startup (crash leftover: no detail rows to reconcile, ids sample=%s)",
            key_frozen_ids,
        )
        await db.execute(update(ApiKey).where(ApiKey.budget_frozen > 0).values(budget_frozen=Decimal("0")))
    await db.execute(update(Wallet).values(frozen_balance=Decimal("0")))
    await db.execute(
        update(BalanceHold)
        .where(BalanceHold.status == "frozen")
        .values(status="released", close_reason="startup", closed_at=now)
    )
    await db.commit()
