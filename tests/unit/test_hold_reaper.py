"""hold reaper 单测：过期悬空冻结的回收编排（宽限/在途跳过/幂等）。

资金对称性由既有 billing 测试覆盖（_close_hold/_release_balances 原语）；
本文件验 reaper 的筛选与调用编排——BillingService 原语打桩记录调用。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from llmpod.apps.proxyserver import hold_reaper
from llmpod.apps.proxyserver.billing import BillingService
from llmpod.models.balance_hold import BalanceHold


def _hold(request_id="r1", status="frozen", expires_at=None, est=Decimal("1.5")):
    return BalanceHold(
        tenant_id=5,
        api_key_id=9,
        route_id=1,
        request_id=request_id,
        est_amount=est,
        status=status,
        expires_at=expires_at,
    )


def _db_factory(session):
    @asynccontextmanager
    async def _fake():
        yield session

    return _fake


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _SelectOnlySession:
    """AND 等值/比较条件过滤的 select + commit（资金原语走桩）。

    条件按 ``operator`` 语义逐条求值（eq/in/gt/lt），AND 链摊平逐条过滤；
    SQL 侧已下推的过期判定（``expires_at < cutoff``）与状态过滤都在这里复算。
    """

    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    async def execute(self, stmt):
        out = self.rows
        for conds in [self._flatten(getattr(stmt, "whereclause", None))]:
            for cond in conds:
                matched = []
                for o in out:
                    if self._matches(o, cond):
                        matched.append(o)
                out = matched
        limit = getattr(stmt, "_limit_clause", None)
        if limit is not None:
            out = out[: int(limit.value)]
        return _FakeResult(out)

    @staticmethod
    def _flatten(node):
        if node is None:
            return []
        clauses = getattr(node, "clauses", None)
        if clauses is not None:
            flat: list = []
            for c in clauses:
                flat.extend(_SelectOnlySession._flatten(c))
            return flat
        return [node]

    @staticmethod
    def _matches(obj, cond) -> bool:
        import operator as _op

        left = getattr(cond, "left", None)
        if left is None:
            return True
        key = getattr(left, "key", None)
        if key is None:
            return True
        lval = getattr(obj, key, None)
        right = getattr(cond, "right", None)
        rval = getattr(right, "effective_value", None)
        if rval is None:
            rval = getattr(right, "value", right)
        fn = getattr(cond, "operator", None)
        if fn is _op.eq:
            return lval == rval
        if fn is _op.ne:
            return lval != rval
        if fn is _op.lt:
            return lval is not None and lval < rval
        if fn is _op.le:
            return lval is not None and lval <= rval
        if fn is _op.gt:
            return lval is not None and lval > rval
        if fn is _op.ge:
            return lval is not None and lval >= rval
        if getattr(fn, "__name__", "") == "in_op":
            return lval in (rval or [])
        return True

    async def commit(self):
        self.commits += 1


@pytest.fixture
def release_calls(monkeypatch):
    calls: list[dict] = []

    async def _close_hold(self, db, *, request_id, status, close_reason=None):
        calls.append({"request_id": request_id, "status": status, "close_reason": close_reason})
        return True

    async def _release_balances(self, db, *, tenant_id, key_id, est):
        calls.append({"release": est, "tenant_id": tenant_id, "key_id": key_id})
        return Decimal("0")

    monkeypatch.setattr(BillingService, "_close_hold", _close_hold)
    monkeypatch.setattr(BillingService, "_release_balances", _release_balances)
    return calls


async def test_releases_expired_beyond_grace(release_calls):
    now = datetime.now(timezone.utc)
    stale = _hold(request_id="r1", expires_at=now - timedelta(hours=3))  # 过期 3h > 宽限 1h
    session = _SelectOnlySession([stale])
    n = await hold_reaper.reap_expired_holds(_db_factory(session), set())
    assert n == 1
    assert release_calls[0]["status"] == "released" and release_calls[0]["close_reason"] == "reaper"
    assert release_calls[1]["release"] == Decimal("1.5")


async def test_within_grace_skipped(release_calls):
    now = datetime.now(timezone.utc)
    fresh = _hold(request_id="r1", expires_at=now - timedelta(minutes=10))  # 过期 10min < 宽限
    session = _SelectOnlySession([fresh])
    n = await hold_reaper.reap_expired_holds(_db_factory(session), set())
    assert n == 0 and release_calls == []


async def test_active_request_skipped(release_calls):
    now = datetime.now(timezone.utc)
    stale = _hold(request_id="r-live", expires_at=now - timedelta(hours=3))
    session = _SelectOnlySession([stale])
    n = await hold_reaper.reap_expired_holds(_db_factory(session), {"r-live"})
    assert n == 0 and release_calls == []


async def test_settled_and_null_expiry_skipped(release_calls):
    now = datetime.now(timezone.utc)
    session = _SelectOnlySession(
        [
            _hold(request_id="r-settled", status="settled", expires_at=now - timedelta(hours=3)),
            _hold(request_id="r-noexp", expires_at=None),
        ]
    )
    n = await hold_reaper.reap_expired_holds(_db_factory(session), set())
    assert n == 0 and release_calls == []


async def test_zero_amount_closes_without_release(release_calls):
    now = datetime.now(timezone.utc)
    stale = _hold(request_id="r-zero", expires_at=now - timedelta(hours=3), est=Decimal("0"))
    session = _SelectOnlySession([stale])
    n = await hold_reaper.reap_expired_holds(_db_factory(session), set())
    assert n == 1
    assert release_calls == [{"request_id": "r-zero", "status": "released", "close_reason": "reaper"}]


async def test_claim_failure_not_counted(monkeypatch):
    """认领失败（并发已被结算）→ 该笔跳过，不计入回收数。"""
    now = datetime.now(timezone.utc)
    stale = _hold(request_id="r1", expires_at=now - timedelta(hours=3))
    session = _SelectOnlySession([stale])

    async def _close_hold(self, db, *, request_id, status, close_reason=None):
        return False  # 行已非 frozen

    monkeypatch.setattr(BillingService, "_close_hold", _close_hold)
    n = await hold_reaper.reap_expired_holds(_db_factory(session), set())
    assert n == 0
