"""tenant_dashboard_service 聚合单测：单次取行共用 / 封禁排除 / 月消费取绝对值 / 装配形状。

聚合 service 的语句文本分发桩见 tests/unit/_fakes.FakeDB（FakeSession 不支持
func.sum 聚合与多列 select）。同时捕获语句文本断言租户过滤下推。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from llmpod.services import metric_query_service
from llmpod.services import tenant_dashboard_service as svc
from llmpod.services.tenant_dashboard_service import tenant_screen

from tests.unit._fakes import TZ, FakeDB, rollup_row


def _now():
    return datetime(2026, 9, 5, 15, 0, tzinfo=TZ)


def _wallet(balance, frozen):
    return SimpleNamespace(
        tenant_id=5,
        balance=Decimal(balance),
        frozen_balance=Decimal(frozen),
    )


def _make_db(*, rollup_rows=(), blocked=(), routes=(), request_logs=(), wallet=None, month_sum=None):
    return FakeDB(
        {
            "metric_rollup": rollup_rows,
            # select(TenantModelBlock.route_id) → 标量流（仅 active 封禁集）
            "tenant_model_block": list(blocked),
            # select(Route.id, Route.name) 多列 → 元组行
            "route": routes,
            "request_log": request_logs,
            "wallet": [wallet] if wallet else [],
            "balance_transaction": [month_sum] if month_sum is not None else [],
        }
    )


class TestTenantScreen:
    async def test_assembly_and_scoping(self, monkeypatch):
        """整屏装配：指标卡聚合 / 模型排除封禁 / 月消费取绝对值 / 余额三段 / 单次取行。"""
        monkeypatch.setattr(svc, "now_local", _now)
        db = _make_db(
            rollup_rows=[
                rollup_row(requests=100, model_alias="deepseek", tenant_id=5),
                rollup_row(requests=2, status="error", model_alias="deepseek", tenant_id=5),
            ],
            blocked=[2],  # active 封禁 route 2（disabled 封禁不进桩的 active 集）
            routes=[(1, "DeepSeek-V3"), (2, "Qwen3"), (3, "GLM")],
            request_logs=[("qwen-max", 10, 20, 320, "success", datetime(2026, 9, 5, 12, 0, tzinfo=TZ))],
            wallet=_wallet("234.50", "100.00"),
            month_sum=Decimal("-89.20"),
        )

        out = await tenant_screen(db, 5, from_=None, to=_now())

        # 指标卡：触达上游 102、错误率 2/102、活跃模型 1
        assert out["calls"] == 102
        assert out["error_rate"] == round(2 / 102, 4)
        assert out["active_models"] == 1
        # 趋势：近 7 日逐日、空日补 0
        assert len(out["trend"]) == 7
        assert sum(p["call_count"] for p in out["trend"]) == 102
        # 模型：排除 active 封禁（route 2）
        names = [m["name"] for m in out["models"]]
        assert names == ["DeepSeek-V3", "GLM"]
        # 最近调用形状
        assert out["recent_calls"][0]["model"] == "qwen-max"
        assert out["recent_calls"][0]["latency_ms"] == 320
        # 余额三段 + 月消费（consume 记负数 → 取绝对值）
        assert out["available"] == Decimal("134.50")
        assert out["month_consume"] == Decimal("89.20")

        # 指标卡与趋势共用一次取行，且租户过滤下推
        rollup_stmts = [s for s in db.statements if "FROM metric_rollup" in s]
        assert len(rollup_stmts) == 1  # 单次取行（并集窗口）
        assert "tenant_id" in rollup_stmts[0]

    async def test_empty_data_zero_fallbacks(self, monkeypatch):
        """无 rollup / 无钱包 / 无消费 → 指标零值 + 余额三段按 0。"""
        monkeypatch.setattr(svc, "now_local", _now)
        db = _make_db()
        out = await tenant_screen(db, 5, from_=None, to=_now())
        assert out["calls"] == 0 and out["error_rate"] == 0.0
        assert out["avg_ttft_ms"] is None
        assert out["models"] == [] and out["recent_calls"] == []
        assert out["balance"] == Decimal("0")
        assert out["month_consume"] == Decimal("0")

    async def test_card_respects_explicit_to(self, monkeypatch):
        """显式 to 早于当前：取行上界为 max(to, now)（趋势要取到当前），
        指标卡须自带上界切回 [from_, to)，(to, now] 的桶行只进趋势不进卡片。"""
        monkeypatch.setattr(svc, "now_local", _now)
        db = _make_db(
            rollup_rows=[
                rollup_row(requests=10, bucket_start=datetime(2026, 9, 5, 10, 0, tzinfo=TZ)),
                rollup_row(requests=5, bucket_start=datetime(2026, 9, 5, 14, 30, tzinfo=TZ)),
            ],
        )
        out = await tenant_screen(db, 5, from_=None, to=datetime(2026, 9, 5, 12, 0, tzinfo=TZ))
        assert out["calls"] == 10
        assert sum(p["call_count"] for p in out["trend"]) == 15

    async def test_naive_window_no_type_error(self, monkeypatch):
        """无时区后缀的 to（FastAPI 对无偏移 ISO 串的解析形态）：窗口归一后整屏
        可装配——归一前 naive from_ 与 aware trend_from 在 min/max 里比较会抛
        TypeError（租户大屏必现 500 的回归锁定）。"""
        monkeypatch.setattr(svc, "now_local", _now)
        monkeypatch.setattr(metric_query_service, "now_local", _now)
        db = _make_db(
            rollup_rows=[
                rollup_row(requests=10, tenant_id=5, bucket_start=datetime(2026, 9, 5, 10, 0, tzinfo=TZ)),
            ]
        )
        out = await tenant_screen(db, 5, from_=None, to=datetime(2026, 9, 5, 15, 0))
        assert out["calls"] == 10
