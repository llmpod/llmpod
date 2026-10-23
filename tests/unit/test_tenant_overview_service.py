"""tenant_overview 聚合单测：日界映射 / 负数流水取绝对值 / 空日补零 / top-n 排序。

FakeSession 不支持带表达式的 group-by 聚合，此处用 unit/_fakes.FakeDB 按语句形状
分发 canned 结果行直接喂聚合，专测聚合数学与装配。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tests.unit._fakes import FakeDB

from llmpod.services import tenant_service
from llmpod.services.tenant_service import tenant_overview

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai 固定偏移，测试环境无 tzdata 依赖


def _now():
    return datetime(2026, 9, 5, 15, 0, tzinfo=TZ)


def _idx(dt: datetime) -> int:
    """与服务同构的「本地日序号」：epoch + 时区偏移 后按日取整。"""
    return int((dt.timestamp() + 8 * 3600) // 86400)


def _db(*, tenants=(), user_count=0, tx_rows=(), wallets=()):
    """装配 FakeDB：租户行 / 用户计数 / 流水聚合 / 钱包行按 FROM 表名分发。"""
    return FakeDB(
        {
            "tenant": [t for t in tenants if t.id != 0],  # 服务侧 where(Tenant.id != 0) 的等价模拟
            '"user"': [user_count],
            "balance_transaction": list(tx_rows),
            "wallet": list(wallets),
        }
    )


class _TenantRow:
    def __init__(self, id, name, status):
        self.id, self.name, self.status = id, name, status


class _WalletRow:
    def __init__(self, tenant_id, balance, frozen):
        self.tenant_id, self.balance, self.frozen_balance = tenant_id, balance, frozen


class TestTenantOverview:
    async def test_counts_and_users(self, monkeypatch):
        monkeypatch.setattr(tenant_service, "now_local", _now)
        db = _db(
            tenants=[
                _TenantRow(1, "甲", "active"),
                _TenantRow(2, "乙", "active"),
                _TenantRow(3, "丙", "disabled"),
                _TenantRow(0, "平台", "active"),
            ],
            user_count=69,
        )
        out = await tenant_overview(db, days=3, top_n=5)
        # 平台租户(id=0)不计入租户口径
        assert (out["total_tenants"], out["active_tenants"], out["disabled_tenants"]) == (3, 2, 1)
        assert out["total_users"] == 69
        assert len(out["dates"]) == 3 and out["dates"][-1] == "2026-09-05"
        assert out["consume_trend"] == [] and out["balance_top"] == []

    async def test_consume_trend_day_mapping_and_abs(self, monkeypatch):
        """consume 记负数 → 日消费取绝对值；日序号相减映射到窗口下标。"""
        monkeypatch.setattr(tenant_service, "now_local", _now)
        # 9/4 本地 23:00（= UTC 9/4 15:00）消费 -100；9/5 本地 01:00 消费 -50.5
        d1 = datetime(2026, 9, 4, 23, 0, tzinfo=TZ)
        d2 = datetime(2026, 9, 5, 1, 0, tzinfo=TZ)
        db = _db(
            tenants=[_TenantRow(1, "甲", "active"), _TenantRow(2, "乙", "active")],
            tx_rows=[(1, _idx(d1), Decimal("-100")), (1, _idx(d2), Decimal("-50.5"))],
        )
        out = await tenant_overview(db, days=2, top_n=5)
        assert out["dates"] == ["2026-09-04", "2026-09-05"]
        assert out["consume_trend"] == [
            {"tenant_id": 1, "tenant_name": "甲", "data": [100.0, 50.5]},
        ]

    async def test_trend_topn_ranking_and_zero_fill(self, monkeypatch):
        monkeypatch.setattr(tenant_service, "now_local", _now)
        today = datetime(2026, 9, 5, 12, 0, tzinfo=TZ)
        yesterday = today - timedelta(days=1)
        db = _db(
            tenants=[_TenantRow(1, "甲", "active"), _TenantRow(2, "乙", "active"), _TenantRow(3, "丙", "active")],
            tx_rows=[
                (1, _idx(yesterday), Decimal("-10")),
                (2, _idx(today), Decimal("-20")),  # 乙窗口总额最高 → 排第一
                (3, _idx(today), Decimal("-5")),
            ],
        )
        out = await tenant_overview(db, days=2, top_n=2)
        assert [t["tenant_id"] for t in out["consume_trend"]] == [2, 1]  # 丙被 top_n 截掉
        assert out["consume_trend"][0]["data"] == [0.0, 20.0]  # 昨天空日补 0

    async def test_balance_top_ranked_by_available(self, monkeypatch):
        monkeypatch.setattr(tenant_service, "now_local", _now)
        db = _db(
            tenants=[_TenantRow(1, "甲", "active"), _TenantRow(2, "乙", "active"), _TenantRow(3, "丙", "active")],
            wallets=[
                _WalletRow(1, Decimal("100"), Decimal("30")),  # available 70
                _WalletRow(2, Decimal("80"), Decimal("0")),  # available 80 → 第一
                _WalletRow(3, Decimal("500"), Decimal("500")),  # available 0 → 出局(top2)
            ],
        )
        out = await tenant_overview(db, days=3, top_n=2)
        assert [(b["tenant_id"], b["tenant_name"], str(b["available"])) for b in out["balance_top"]] == [
            (2, "乙", "80"),
            (1, "甲", "70"),
        ]
