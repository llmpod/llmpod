"""wallet_service 集成测试（FakeSession + 真实 service 逻辑）。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from _fakes import FakeSession

from llmpod.core.exceptions import LLMPodValidationError
from llmpod.models import BalanceTransaction, Wallet
from llmpod.services.wallet_service import adjust_wallet, get_balance, list_transactions, list_wallets


class TestAdjustWallet:
    async def test_add_creates_wallet_and_ledger(self):
        db = FakeSession()
        wallet = await adjust_wallet(db, tenant_id=5, amount=Decimal("100"), mode="add")
        assert wallet.balance == Decimal("100")
        assert len(db.all(Wallet)) == 1
        txns = db.all(BalanceTransaction)
        assert len(txns) == 1
        assert txns[0].type == "topup"  # 充值写 topup（非 adjust）
        assert txns[0].amount == Decimal("100")
        assert txns[0].balance_after == Decimal("100")

    async def test_subtract(self):
        db = FakeSession()
        await adjust_wallet(db, tenant_id=5, amount=Decimal("100"), mode="add")
        wallet = await adjust_wallet(db, tenant_id=5, amount=Decimal("30"), mode="subtract")
        assert wallet.balance == Decimal("70")
        txns = sorted(db.all(BalanceTransaction), key=lambda t: t.id or 0)
        assert txns[-1].amount == Decimal("-30")
        assert txns[-1].balance_after == Decimal("70")

    async def test_override(self):
        db = FakeSession()
        await adjust_wallet(db, tenant_id=5, amount=Decimal("100"), mode="add")
        wallet = await adjust_wallet(db, tenant_id=5, amount=Decimal("42"), mode="override")
        assert wallet.balance == Decimal("42")
        txns = sorted(db.all(BalanceTransaction), key=lambda t: t.id or 0)
        assert txns[-1].amount == Decimal("-58")  # 42 - 100

    async def test_invalid_mode(self):
        db = FakeSession()
        with pytest.raises(LLMPodValidationError):
            await adjust_wallet(db, tenant_id=5, amount=Decimal("1"), mode="bogus")

    async def test_add_requires_positive(self):
        db = FakeSession()
        with pytest.raises(LLMPodValidationError):
            await adjust_wallet(db, tenant_id=5, amount=Decimal("0"), mode="add")


class TestGetBalance:
    async def test_missing_wallet_returns_zero(self):
        db = FakeSession()
        assert await get_balance(db, 5) == Decimal("0")


class TestListTransactions:
    async def test_lists_and_paginates(self):
        db = FakeSession()
        for _ in range(3):
            await adjust_wallet(db, tenant_id=5, amount=Decimal("10"), mode="add")
        rows, total = await list_transactions(db, tenant_id=5, page=1, page_size=2)
        assert total == 3
        assert len(rows) == 2
        assert rows[0].id > rows[1].id  # 倒序：最新 id 在前


class TestListWallets:
    async def test_sorts_negative_first(self):
        db = FakeSession()
        await adjust_wallet(db, tenant_id=1, amount=Decimal("10"), mode="add")
        await adjust_wallet(db, tenant_id=2, amount=Decimal("5"), mode="subtract")  # 余额 -5
        await adjust_wallet(db, tenant_id=3, amount=Decimal("100"), mode="add")
        wallets = await list_wallets(db)
        assert wallets[0].tenant_id == 2  # 负余额在前
        assert wallets[0].balance == Decimal("-5")
