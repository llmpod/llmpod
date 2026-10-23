"""BillingService 编排测试（冻结预扣 / 结算 / 解冻 + 负余额告警，SQL 原语桩替换）。

冻结语义：hold 只冻 ``frozen_balance``（balance 不动）、settle 真正扣 balance 并释放
冻结、release 只释放冻结不写流水。hold 生命周期落 balance_hold（frozen → settled/released）。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from llmpod.apps.proxyserver.authn import ApiKeyContext
from llmpod.apps.proxyserver.billing import (
    BillingService,
    HeldReservation,
    InsufficientBalance,
    KeyBudgetExceeded,
)
from llmpod.apps.proxyserver.snapshot import RouteEntry


def _ctx(tenant_id=5, key_id=1, key_name="k1", budget_limit=None) -> ApiKeyContext:
    return ApiKeyContext(
        key_id=key_id,
        tenant_id=tenant_id,
        allowed_models=frozenset({"m1"}),
        rpm_limit=None,
        tpm_limit=None,
        total_token_quota=None,
        model_token_quotas={},
        key_name=key_name,
        budget_limit=budget_limit,
    )


def _entry(route_id=7, name="m1") -> RouteEntry:
    return RouteEntry(
        route_id=route_id,
        name=name,
        status="published",
        input_price_per_mtok=Decimal("2"),
        output_price_per_mtok=Decimal("4"),
        targets=(),
    )


class _FakeDb:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def add(self, obj):
        pass

    async def flush(self):
        pass


class _FakeDbFactory:
    def __call__(self):
        return _FakeDb()


class _RecordingBilling(BillingService):
    def __init__(
        self,
        *,
        freeze_raises=None,
        balance_after=Decimal("0"),
        spend_raises=None,
        spend_rowcount=1,
    ):
        super().__init__(db_factory=_FakeDbFactory())
        self._freeze_raises = freeze_raises
        self._balance_after = balance_after
        self._spend_raises = spend_raises
        self._spend_rowcount = spend_rowcount
        self.freeze_calls = []
        self.platform_freeze_calls = []
        self.platform_unfreeze_calls = []
        self.settle_calls = []
        self.release_calls = []
        self.spend_calls = []
        self.hold_rows = []
        self.closed = []
        self.ledger_rows = []
        self._hold_status = {}

    async def _freeze(self, db, *, key_id, tenant_id, amount):
        self.freeze_calls.append((key_id, tenant_id, amount))
        if self._freeze_raises:
            raise self._freeze_raises

    async def _freeze_key_budget(self, db, *, key_id, amount):
        self.platform_freeze_calls.append((key_id, amount))

    async def _release_key_budget(self, db, *, key_id, est):
        self.platform_unfreeze_calls.append((key_id, est))

    async def _settle_key_budget(self, db, *, key_id, amount, est):
        self.spend_calls.append((key_id, amount))
        if self._spend_raises:
            raise self._spend_raises
        return SimpleNamespace(rowcount=self._spend_rowcount)

    async def _settle_balances(self, db, *, tenant_id, key_id, actual, est):
        self.settle_calls.append((tenant_id, key_id, actual, est))
        return self._balance_after

    async def _release_balances(self, db, *, tenant_id, key_id, est):
        self.release_calls.append((tenant_id, key_id, est))
        return self._balance_after

    async def _insert_hold(self, db, *, tenant_id, key_id, route_id, request_id, est_amount, expires_at):
        self.hold_rows.append(
            {
                "tenant_id": tenant_id,
                "key_id": key_id,
                "route_id": route_id,
                "request_id": request_id,
                "est_amount": est_amount,
                "expires_at": expires_at,
            }
        )

    async def _close_hold(self, db, *, request_id, status, close_reason=None):
        if request_id in self._hold_status:
            return False
        self._hold_status[request_id] = status
        self.closed.append({"request_id": request_id, "status": status, "close_reason": close_reason})
        return True

    async def _insert_ledger(self, db, **kw):
        self.ledger_rows.append(kw)


class TestHold:
    @pytest.mark.parametrize("est", [Decimal("0.1"), Decimal("0")], ids=["normal", "zero-estimate"])
    async def test_platform_key_guard_without_wallet(self, est):
        """平台 Key 不冻钱包、不写 balance_hold，但恒返回 guard——结算链路靠它驱动。

        鉴权快照有上限 + 估算非零才进预算冻结（budget_frozen 占位）。zero-estimate
        是回归锁：零成本短路只对租户路径成立，未配定价时平台 Key 的真实消费同样要
        记账（按短路写就会让该次调用永不入账）。
        """
        b = _RecordingBilling()
        held = await b.hold(_ctx(tenant_id=None, budget_limit=Decimal("1")), _entry(), est, request_id="r1")
        assert held is not None and held.est_cost == est
        assert b.freeze_calls == [] and b.hold_rows == []  # 钱包侧零动作
        if est > 0:
            assert b.platform_freeze_calls == [(1, est)]
        else:
            assert b.platform_freeze_calls == []

    async def test_platform_key_no_limit_skips_db_entirely(self):
        """鉴权快照无上限（NULL=不限）：预扣级零 DB 写——行锁 UPDATE 每请求一次
        纯属浪费，共享的管理端调试 Key 高频转发也不该串行在单行上。"""
        b = _RecordingBilling()
        held = await b.hold(_ctx(tenant_id=None, budget_limit=None), _entry(), Decimal("0.1"), request_id="r1")
        assert held is not None and held.est_cost == Decimal("0.1")
        assert b.platform_freeze_calls == [] and b.freeze_calls == [] and b.hold_rows == []

    async def test_skips_zero_cost(self):
        b = _RecordingBilling()
        held = await b.hold(_ctx(), _entry(), Decimal("0"), request_id="r1")
        assert held is None
        assert b.freeze_calls == [] and b.hold_rows == []

    async def test_freezes_and_records_hold(self):
        b = _RecordingBilling()
        held = await b.hold(_ctx(tenant_id=5, key_id=1), _entry(), Decimal("0.1"), request_id="r1")
        assert b.freeze_calls == [(1, 5, Decimal("0.1"))]
        assert held is not None and held.est_cost == Decimal("0.1")
        assert len(b.hold_rows) == 1
        row = b.hold_rows[0]
        assert row["request_id"] == "r1"
        assert row["tenant_id"] == 5 and row["key_id"] == 1 and row["route_id"] == 7
        assert row["est_amount"] == Decimal("0.1")
        assert row["expires_at"] is not None  # 预留租约时间，本次不消费

    async def test_propagates_insufficient(self):
        b = _RecordingBilling(freeze_raises=InsufficientBalance("insufficient balance"))
        with pytest.raises(InsufficientBalance):
            await b.hold(_ctx(), _entry(), Decimal("0.1"), request_id="r1")

    async def test_platform_key_budget_exceeded_propagates(self):
        """平台 Key 预算超限同样向上抛 KeyBudgetExceeded（路由层转 402）——
        冻结原语是唯一判定点，编排层不得吞。"""
        b = _RecordingBilling()

        async def deny(db, *, key_id, amount):
            raise KeyBudgetExceeded("key budget exceeded")

        b._freeze_key_budget = deny
        with pytest.raises(KeyBudgetExceeded):
            await b.hold(_ctx(tenant_id=None, budget_limit=Decimal("1")), _entry(), Decimal("0.1"), request_id="r1")


class TestSettle:
    async def test_same_request_cannot_debit_twice_after_guard_is_recreated(self):
        b = _RecordingBilling()
        await b.settle(_ctx(), _entry(), Decimal("0.03"), Decimal("0.1"), request_id="r1")
        await b.settle(_ctx(), _entry(), Decimal("0.03"), Decimal("0.1"), request_id="r1")
        await b.release(_ctx(), _entry(), Decimal("0.1"), request_id="r1")
        assert len(b.settle_calls) == len(b.ledger_rows) == 1
        assert b.release_calls == []

    async def test_platform_key_records_spend_only(self):
        """平台 Key 不扣钱包、不写流水、不关冻结行；记消费 + 释放预算占位。"""
        b = _RecordingBilling()
        await b.settle(
            _ctx(tenant_id=None), _entry(), actual_cost=Decimal("0.1"), est_cost=Decimal("0.1"), request_id="r1"
        )
        assert b.spend_calls == [(1, Decimal("0.1"))]
        assert b.settle_calls == [] and b.ledger_rows == [] and b.closed == []
        assert b.platform_unfreeze_calls == []  # 占位释放并入 _settle_key_budget 原语，不走独立解冻

    @pytest.mark.parametrize("actual", [None, Decimal("0")])
    async def test_platform_key_zero_actual_writes_nothing(self, actual):
        """无 usage / 零费用且无占位：不产生空写（0 元 UPDATE 无意义）。"""
        b = _RecordingBilling()
        await b.settle(_ctx(tenant_id=None), _entry(), actual_cost=actual, est_cost=Decimal("0"), request_id="r1")
        assert b.spend_calls == []

    async def test_platform_key_zero_actual_releases_frozen(self):
        """无 usage 但有预扣占位（设了上限的 Key）：消费按 0 记、budget_frozen 占位
        必须释放（skip 会悬空到重启，之后同 Key 请求被 phantom 冻结误拒）。
        无上限 Key 无占位可放，零消费零占位直接免写（skip 分支另由 no-limit 用例锁）。"""
        b = _RecordingBilling()
        await b.settle(
            _ctx(tenant_id=None, budget_limit=Decimal("1")),
            _entry(),
            actual_cost=None,
            est_cost=Decimal("0.1"),
            request_id="r1",
        )
        assert b.spend_calls == [(1, Decimal("0"))]

    async def test_platform_key_negative_actual_recorded(self):
        """符号口径与租户路径一致：负值（调减）照样入账，只有 0 被跳过。"""
        b = _RecordingBilling()
        await b.settle(
            _ctx(tenant_id=None), _entry(), actual_cost=Decimal("-0.02"), est_cost=Decimal("0.1"), request_id="r1"
        )
        assert b.spend_calls == [(1, Decimal("-0.02"))]

    async def test_platform_key_spend_failure_logs_then_raises(self, caplog):
        """写入失败：打 ERROR 留全可复算字段后上抛——guard 捕获后回退 release 解冻占位；
        吞掉会让 _settled 误置位、占位悬空到重启（无内置补偿：紧跟着的 UPDATE 大概率同败）。"""
        b = _RecordingBilling(spend_raises=RuntimeError("pg down"))
        with caplog.at_level(logging.ERROR, logger="llmpod.proxyserver.billing"):
            with pytest.raises(RuntimeError):
                await b.settle(
                    _ctx(tenant_id=None),
                    _entry(),
                    actual_cost=Decimal("0.1"),
                    est_cost=Decimal("0.1"),
                    request_id="r1",
                )
        assert "platform key spend dropped" in caplog.text
        assert "key=1" in caplog.text and "0.1" in caplog.text and "r1" in caplog.text
        assert b.platform_unfreeze_calls == []  # 解冻由 guard 的 release 兜底，不在 settle 内做

    async def test_platform_key_settle_failure_preserves_hold(self):
        """已产出后结算失败，不能解冻并免费放行。"""
        b = _RecordingBilling(spend_raises=RuntimeError("pg down"))
        held = await b.hold(_ctx(tenant_id=None, budget_limit=Decimal("1")), _entry(), Decimal("0.1"), request_id="r1")
        with pytest.raises(RuntimeError):
            await held.settle(Decimal("0.03"), request_id="r1")
        await held.release(request_id="r1")
        assert b.platform_unfreeze_calls == []

    async def test_platform_key_spend_zero_row_logs_reconcilable_error(self, caplog):
        """Key 行在 admit 与 settle 之间被删：UPDATE 零行也 commit 成功、异常分支不触发，
        不查 rowcount 这笔消费就零线索——与写入失败同口径打 ERROR。"""
        b = _RecordingBilling(spend_rowcount=0)
        with caplog.at_level(logging.ERROR, logger="llmpod.proxyserver.billing"):
            with pytest.raises(RuntimeError, match="platform key row missing"):
                await b.settle(
                    _ctx(tenant_id=None), _entry(), actual_cost=Decimal("0.1"), est_cost=Decimal("0.1"), request_id="r1"
                )
        assert "platform key spend dropped" in caplog.text
        assert b.spend_calls == [(1, Decimal("0.1"))]  # 原语确实执行过

    async def test_missing_usage_settles_zero(self):
        """actual=None 按 0 元结算——balance 不变、frozen 全释放、consume 金额 0。"""
        b = _RecordingBilling(balance_after=Decimal("1.0"))
        await b.settle(_ctx(), _entry(), actual_cost=None, est_cost=Decimal("0.1"), request_id="r1")
        assert b.settle_calls == [(5, 1, Decimal("0"), Decimal("0.1"))]
        row = b.ledger_rows[0]
        assert row["txn_type"] == "consume"
        assert row["amount"] == Decimal("0")

    async def test_settles_actual_and_closes_hold(self):
        b = _RecordingBilling(balance_after=Decimal("0.5"))
        await b.settle(_ctx(), _entry(), actual_cost=Decimal("0.03"), est_cost=Decimal("0.1"), request_id="r1")
        assert b.settle_calls == [(5, 1, Decimal("0.03"), Decimal("0.1"))]
        assert len(b.ledger_rows) == 1
        row = b.ledger_rows[0]
        assert row["txn_type"] == "consume"
        assert row["amount"] == Decimal("-0.03")
        assert row["balance_after"] == Decimal("0.5")
        assert row["request_id"] == "r1"
        assert row["route_name"] == "m1"
        assert row["key_name"] == "k1"
        # hold 关闭为 settled
        assert b.closed == [{"request_id": "r1", "status": "settled", "close_reason": None}]

    async def test_negative_balance_logs_error(self, caplog):
        b = _RecordingBilling(balance_after=Decimal("-0.5"))
        with caplog.at_level(logging.ERROR, logger="llmpod.proxyserver.billing"):
            await b.settle(_ctx(), _entry(), actual_cost=Decimal("0.1"), est_cost=Decimal("0.05"), request_id="r1")
        assert "negative balance" in caplog.text


class TestRelease:
    async def test_skips_zero_est(self):
        b = _RecordingBilling()
        await b.release(_ctx(), _entry(), Decimal("0"), request_id="r1")
        assert b.release_calls == [] and b.ledger_rows == [] and b.closed == []

    async def test_platform_key_releases_budget_only(self):
        """平台 Key 解冻只释放 Key 预算占位：不动钱包、不写流水、不关冻结行。"""
        b = _RecordingBilling()
        await b.release(_ctx(tenant_id=None, budget_limit=Decimal("1")), _entry(), Decimal("0.1"), request_id="r1")
        assert b.platform_unfreeze_calls == [(1, Decimal("0.1"))]
        assert b.release_calls == [] and b.ledger_rows == [] and b.closed == []

    async def test_platform_key_no_limit_release_skips_db(self):
        """无上限的平台 Key 预扣没冻结过：解冻同样免进 DB（与 hold 的跳过对称）。"""
        b = _RecordingBilling()
        await b.release(_ctx(tenant_id=None, budget_limit=None), _entry(), Decimal("0.1"), request_id="r1")
        assert b.platform_unfreeze_calls == [] and b.release_calls == [] and b.closed == []

    async def test_releases_frozen_without_ledger(self):
        """解冻只释放 frozen、不写流水；hold 置 released。"""
        b = _RecordingBilling(balance_after=Decimal("1.0"))
        await b.release(_ctx(), _entry(), Decimal("0.1"), request_id="r1")
        assert b.release_calls == [(5, 1, Decimal("0.1"))]
        assert b.ledger_rows == []  # 无 refund/consume 流水
        assert b.closed == [{"request_id": "r1", "status": "released", "close_reason": "upstream_error"}]


class TestHeldReservation:
    """guard 语义：幂等结算 / 流式交付后 release no-op / 结算失败仍可解冻。"""

    async def test_settle_idempotent(self):
        b = _RecordingBilling(balance_after=Decimal("0.5"))
        held = HeldReservation(b, _ctx(), _entry(), Decimal("0.1"))
        await held.settle(Decimal("0.03"))
        await held.settle(Decimal("0.03"))  # 重复调用静默跳过，不二次调账
        assert len(b.settle_calls) == 1

    async def test_mark_streaming_blocks_release(self):
        b = _RecordingBilling()
        held = HeldReservation(b, _ctx(), _entry(), Decimal("0.1"))
        held.mark_streaming()
        await held.release()
        assert b.release_calls == []  # 流式已交付，release 变 no-op

    async def test_mark_streaming_then_settle_still_works(self):
        b = _RecordingBilling(balance_after=Decimal("0.5"))
        held = HeldReservation(b, _ctx(), _entry(), Decimal("0.1"))
        held.mark_streaming()
        await held.settle(Decimal("0.03"))  # 流式生成器 finally 结算仍生效
        assert len(b.settle_calls) == 1

    async def test_settle_failure_preserves_hold(self):
        b = _RecordingBilling()

        async def boom(db, *, tenant_id, key_id, actual, est):
            raise RuntimeError("pg down")

        b._settle_balances = boom  # 结算抛错（瞬时 PG 抖动）
        held = HeldReservation(b, _ctx(), _entry(), Decimal("0.1"))
        with pytest.raises(RuntimeError):
            await held.settle(Decimal("0.03"))
        await held.release()
        assert len(b.release_calls) == 0

    async def test_platform_key_guard_lifecycle(self):
        """平台 Key 全链路：hold 出 guard（预算占位、不冻钱包）→ settle 记消费
        （占位释放并入结算原语）→ release/close 因已结算 no-op。"""
        b = _RecordingBilling()
        held = await b.hold(_ctx(tenant_id=None, budget_limit=Decimal("1")), _entry(), Decimal("0.1"), request_id="r1")
        await held.settle(Decimal("0.03"), request_id="r1")
        await held.release(request_id="r1")
        await held.close(request_id="r1")
        assert b.platform_freeze_calls == [(1, Decimal("0.1"))]
        assert b.spend_calls == [(1, Decimal("0.03"))]
        assert b.platform_unfreeze_calls == []
        assert b.freeze_calls == [] and b.hold_rows == []
        assert b.release_calls == [] and b.ledger_rows == [] and b.closed == []

    async def test_platform_key_close_without_settle_unfreezes(self):
        """流式响应被丢弃未消费：close 兜底释放预算占位（无结算则占位悬空）。"""
        b = _RecordingBilling()
        held = await b.hold(_ctx(tenant_id=None, budget_limit=Decimal("1")), _entry(), Decimal("0.1"), request_id="r1")
        await held.close(request_id="r1")
        assert b.spend_calls == []
        assert b.platform_unfreeze_calls == [(1, Decimal("0.1"))]
