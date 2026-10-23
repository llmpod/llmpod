"""启动时未结账冻结保留给人工对账，防止结算失败后免费释放。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from _fakes import FakeSession

from llmpod.apps.proxyserver.billing import reset_frozen_state
from llmpod.models import ApiKey, BalanceHold, Wallet


def _seed(db: FakeSession) -> FakeSession:
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    db.preload(
        Wallet(id=1, tenant_id=5, balance=Decimal("100"), frozen_balance=Decimal("3"), created_at=now, updated_at=now)
    )
    db.preload(
        ApiKey(
            id=1,
            key_hash="h",
            tenant_id=5,
            name="k",
            allowed_models=[],
            rpm_limit=None,
            tpm_limit=None,
            total_token_quota=None,
            model_token_quotas={},
            budget_limit=None,
            budget_spent=Decimal("2"),
            budget_frozen=Decimal("3"),
            status="active",
            expires_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    db.preload(
        BalanceHold(
            id=1,
            tenant_id=5,
            api_key_id=1,
            route_id=7,
            request_id="r1",
            est_amount=Decimal("3"),
            status="frozen",
            close_reason=None,
            expires_at=now,
            created_at=now,
            closed_at=None,
        )
    )
    db.preload(
        BalanceHold(
            id=2,
            tenant_id=5,
            api_key_id=1,
            route_id=7,
            request_id="r2",
            est_amount=Decimal("1"),
            status="settled",
            close_reason=None,
            expires_at=now,
            created_at=now,
            closed_at=now,
        )
    )
    return db


class TestResetFrozenState:
    async def test_preserves_unsettled_funds(self, caplog):
        db = _seed(FakeSession())
        await reset_frozen_state(db)

        w = next(iter(db.all(Wallet)))
        assert w.frozen_balance == Decimal("3")
        assert w.balance == Decimal("100")  # 总余额不动

        k = next(iter(db.all(ApiKey)))
        assert k.budget_frozen == Decimal("3")
        assert k.budget_spent == Decimal("2")  # 已消费不动

        holds = {h.request_id: h for h in db.all(BalanceHold)}
        # 消融：旧实现盲目标记 released(startup)，下次请求不再受冻结约束。
        assert holds["r1"].status == "frozen"
        assert holds["r1"].close_reason is None
        assert holds["r1"].closed_at is None
        assert holds["r2"].status == "settled"  # 已 settled 的不受影响
        assert "unsettled billing reservations preserved" in caplog.text

    async def test_clean_state_remains_clean(self):
        db = FakeSession()
        await reset_frozen_state(db)
        assert db.all(BalanceHold) == []

    async def test_platform_key_frozen_cleared_at_startup(self, caplog):
        """平台 Key 的 budget_frozen 无明细行可对账：启动直接清零（单副本部署假设）。"""
        # MiniDB：FakeSession 不支持 ``select(col)`` 标量 + ``>`` 条件 update 的
        # 组合，改用支持运算符树的 MiniDB（tests/unit/_minidb.py）
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
        from _minidb import MiniDB

        db = MiniDB()
        # 复用 _seed 的行集：从 FakeSession 把已构造对象搬进 MiniDB
        src = _seed(FakeSession())
        db.preload(*src.all(Wallet), *src.all(ApiKey), *src.all(BalanceHold))
        # 摘掉钱包侧未结冻结（frozen hold 改 settled、钱包冻结清零），只留 Key 预算悬空
        h = next(iter(db.rows_of(BalanceHold)))
        h.status = "settled"
        w = next(iter(db.rows_of(Wallet)))
        w.frozen_balance = Decimal("0")
        await reset_frozen_state(db)

        k = next(iter(db.rows_of(ApiKey)))
        assert k.budget_frozen == Decimal("0")
        assert k.budget_spent == Decimal("2")  # 已消费不动
        assert "budget_frozen cleared at startup" in caplog.text
