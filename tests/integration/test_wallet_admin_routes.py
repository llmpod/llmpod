"""钱包 admin 路由契约测试（鉴权 + Result 契约，业务逻辑由 wallet_service 测试覆盖）。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from httpx import AsyncClient
from _fakes import NUMERIC_OVERSIZED, admin_session

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.models import Wallet


async def _fake_get_tenant(db, tenant_id):
    """桩：调额/查余额端点只要求租户存在（返回值不被使用），故给个占位对象。"""
    return object()


def _stub_wallet(tenant_id=5, balance="12.5", frozen_balance="0") -> Wallet:
    """桩钱包：WalletOut 由 orm_schema(Wallet) 生成，故直接用 ORM 实例喂满字段。"""
    return Wallet(
        id=1,
        tenant_id=tenant_id,
        balance=Decimal(balance),
        frozen_balance=Decimal(frozen_balance),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class TestWalletRoutes:
    async def test_routes_require_auth(self, client: AsyncClient):
        for method, path in [
            ("get", "/api/v1/admin/wallets"),
            ("get", "/api/v1/admin/tenants/5/wallet"),
            ("get", "/api/v1/admin/tenants/5/wallet/transactions"),
            ("post", "/api/v1/admin/tenants/5/wallet/adjust"),
        ]:
            resp = await client.request(method, path, json={"amount": "1", "mode": "add"})
            assert resp.status_code == 401, f"{method} {path} 应受登录保护"

    async def test_get_balance_returns_result(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        class _StubWallet:
            balance = Decimal("12.5")
            frozen_balance = Decimal("2.5")

        async def _fake_snapshot(db, tenant_id):
            w = _StubWallet()
            return w.balance, w.frozen_balance, w.balance - w.frozen_balance

        # 租户存在性校验（不存在 → 404）：给桩库放一个 tenant 5
        from llmpod.db import get_db
        from llmpod.models import Tenant
        from tests.integration._fakes import FakeSession

        fake_db = FakeSession()
        fake_db.preload(
            Tenant(
                id=5,
                name="租户5",
                status="active",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

        async def _gen():
            yield fake_db

        app.dependency_overrides[get_db] = _gen

        import llmpod.apps.webserver.routes.admin.wallet as route_mod

        monkeypatch.setattr(route_mod, "wallet_snapshot", _fake_snapshot)

        resp = await client.get("/api/v1/admin/tenants/5/wallet")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert body["data"]["tenant_id"] == 5
        assert Decimal(body["data"]["balance"]) == Decimal("12.5")  # pydantic v2 Decimal → 字符串
        assert Decimal(body["data"]["frozen_balance"]) == Decimal("2.5")
        assert Decimal(body["data"]["available"]) == Decimal("10.0")

    async def test_unknown_tenant_404(self, client: AsyncClient):
        """不存在的租户 id 查钱包 → 404（不再返回零钱包）。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        resp = await client.get("/api/v1/admin/tenants/999999/wallet")
        assert resp.status_code == 404
        resp2 = await client.get("/api/v1/admin/tenants/999999/wallet/transactions")
        assert resp2.status_code == 404

    async def test_adjust_rejects_over_range_amount(self, client: AsyncClient):
        """超长金额须拦在请求模型（422 + 字段 loc），不得直达 asyncpg 参数绑定。

        422 本身即证明未触达 service：FastAPI 在请求体校验阶段就拒绝，端点函数根本不执行。
        """
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        resp = await client.post(
            "/api/v1/admin/tenants/5/wallet/adjust",
            json={"amount": NUMERIC_OVERSIZED, "mode": "add"},
        )

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", "amount"]]

    async def test_adjust_accepts_column_max_amount(self, client: AsyncClient, monkeypatch):
        """上界本身是合法值（= wallet.balance / balance_transaction.amount 两列的容量）。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen: dict = {}

        async def _fake_adjust(db, *, tenant_id, amount, mode, reason=None):
            seen.update(amount=amount, mode=mode)
            return _stub_wallet(tenant_id=tenant_id)

        import llmpod.apps.webserver.routes.admin.wallet as route_mod

        monkeypatch.setattr(route_mod, "adjust_wallet", _fake_adjust)
        monkeypatch.setattr(route_mod, "get_tenant", _fake_get_tenant)

        resp = await client.post(
            "/api/v1/admin/tenants/5/wallet/adjust",
            json={"amount": "99999999999999.999999", "mode": "add"},
        )

        assert resp.status_code == 200
        assert seen["amount"] == Decimal("99999999999999.999999")

    async def test_adjust_negative_amount_still_reaches_service(self, client: AsyncClient, monkeypatch):
        """负数是合法的请求体：各 mode 的正负语义由 service 判（回落既有的 LLMPodValidationError → 422）。

        本列只补了上界，不得顺带把下界也收紧——否则负值会在 Pydantic 就被拦成 VALIDATION_ERROR，
        service 根本收不到。故此处锁的是「原样抵达 service」，而非状态码。
        """
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen: dict = {}

        from llmpod.core.exceptions import LLMPodValidationError

        async def _fake_adjust(db, *, tenant_id, amount, mode, reason=None):
            seen["amount"] = amount
            raise LLMPodValidationError("add 金额必须为正")

        import llmpod.apps.webserver.routes.admin.wallet as route_mod

        monkeypatch.setattr(route_mod, "adjust_wallet", _fake_adjust)
        monkeypatch.setattr(route_mod, "get_tenant", _fake_get_tenant)

        resp = await client.post(
            "/api/v1/admin/tenants/5/wallet/adjust",
            json={"amount": "-1", "mode": "add"},
        )

        assert seen["amount"] == Decimal("-1"), "负值必须原样抵达 service，不得被请求模型的下界拦下"
        assert resp.status_code == 422
