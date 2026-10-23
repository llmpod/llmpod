"""request_log_service.py 单测：计量行列表过滤（租户/Key/模型/状态/会话指纹/时间区间）+ 单条查询。

时间区间：since/until 为含边界（>=/<=），naive 入参按 UTC 归一（as_utc）；
毫秒精度（路由层 ms_to_datetime 产物）参与比较。排序 created_at desc + id tie-break。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from llmpod.core.exceptions import NotFound
from llmpod.models import RequestLog
from llmpod.services import request_log_service as svc
from tests.unit._minidb import MiniDB

_T0 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)


def _ts(hours: float) -> datetime:
    return _T0 + timedelta(hours=hours)


@pytest.fixture
def db() -> MiniDB:
    d = MiniDB()
    rows = [
        # id 顺序即插入顺序；r3/r4 同刻 → id tie-break
        RequestLog(
            id=1,
            request_id="rq-1",
            tenant_id=5,
            api_key_id=11,
            model_alias="m1",
            status="success",
            conv_prefix_hash="aaa",
            created_at=_ts(0),
        ),
        RequestLog(
            id=2,
            request_id="rq-2",
            tenant_id=5,
            api_key_id=12,
            model_alias="m2",
            status="error",
            conv_prefix_hash=None,
            created_at=_ts(1),
        ),
        RequestLog(
            id=3,
            request_id="rq-3",
            tenant_id=6,
            api_key_id=11,
            model_alias="m1",
            status="success",
            conv_prefix_hash="aaa",
            created_at=_ts(2),
        ),
        RequestLog(
            id=4,
            request_id="rq-4",
            tenant_id=5,
            api_key_id=11,
            model_alias="m1",
            status="success",
            conv_prefix_hash="bbb",
            created_at=_ts(2),
        ),
    ]
    d.preload(*rows)
    return d


def _ids(rows):
    return [r.id for r in rows]


class TestListFilters:
    async def test_no_filters_returns_all_desc(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20)
        assert total == 4
        assert _ids(rows) == [4, 3, 2, 1]  # 时间倒序

    async def test_same_timestamp_id_tiebreak(self, db):
        rows, _ = await svc.list_request_logs(db, page=1, page_size=20)
        assert _ids(rows)[:2] == [4, 3]  # 同刻行按 id 倒序，序稳定

    async def test_tenant_scope(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, tenant_id=5)
        assert total == 3
        assert _ids(rows) == [4, 2, 1]

    async def test_api_key_filter(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, api_key_id=12)
        assert (total, _ids(rows)) == (1, [2])

    async def test_model_alias_filter_combined_with_tenant(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, tenant_id=5, model="m1")
        assert (total, _ids(rows)) == (2, [4, 1])

    async def test_status_filter(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, status="error")
        assert (total, _ids(rows)) == (1, [2])

    async def test_conv_prefix_hash_filter(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, conv_prefix_hash="aaa")
        assert (total, _ids(rows)) == (2, [3, 1])

    async def test_no_match_zero_total(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, model="nope")
        assert (rows, total) == ([], 0)


class TestListPlatformScope:
    """admin 视角开关：exclude_tenant_scope 在 tenant_id 缺省时只看平台记录（tenant_id 为空）。

    独立 fixture（既有 ``db`` 的 4 行全带 tenant_id，改它会殃及上面全量/分页断言）。
    """

    @pytest.fixture
    def db_platform(self) -> MiniDB:
        d = MiniDB()
        d.preload(
            RequestLog(
                id=1,
                request_id="rq-p1",
                tenant_id=None,  # 平台 Key 计量行
                api_key_id=900,
                model_alias="m1",
                status="success",
                conv_prefix_hash=None,
                created_at=_ts(0),
            ),
            RequestLog(
                id=2,
                request_id="rq-t5",
                tenant_id=5,
                api_key_id=11,
                model_alias="m1",
                status="success",
                conv_prefix_hash=None,
                created_at=_ts(1),
            ),
            RequestLog(
                id=3,
                request_id="rq-p2",
                tenant_id=None,  # 平台 Key 计量行
                api_key_id=900,
                model_alias="m2",
                status="error",
                conv_prefix_hash=None,
                created_at=_ts(2),
            ),
            RequestLog(
                id=4,
                request_id="rq-t6",
                tenant_id=6,
                api_key_id=11,
                model_alias="m1",
                status="success",
                conv_prefix_hash=None,
                created_at=_ts(3),
            ),
        )
        return d

    async def test_platform_scope_returns_only_null_tenant(self, db_platform):
        rows, total = await svc.list_request_logs(db_platform, page=1, page_size=20, exclude_tenant_scope=True)
        assert (total, _ids(rows)) == (2, [3, 1])  # 只平台行，时间倒序

    async def test_platform_scope_combines_with_other_filters(self, db_platform):
        rows, total = await svc.list_request_logs(
            db_platform, page=1, page_size=20, model="m1", exclude_tenant_scope=True
        )
        assert (total, _ids(rows)) == (1, [1])

    async def test_tenant_filter_wins_over_scope_switch(self, db_platform):
        """显式传 tenant_id 时精确看该租户，不产生 IS NULL 限定。"""
        rows, total = await svc.list_request_logs(
            db_platform, page=1, page_size=20, tenant_id=5, exclude_tenant_scope=True
        )
        assert (total, _ids(rows)) == (1, [2])

    async def test_tenant_route_default_keeps_all(self, db_platform):
        """不传开关（租户端用法，另有 tenant_id 强制）：行为不变，平台行不消失。"""
        rows, total = await svc.list_request_logs(db_platform, page=1, page_size=20)
        assert (total, _ids(rows)) == (4, [4, 3, 2, 1])


class TestListTimeRange:
    async def test_since_inclusive(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, since=_ts(1))
        assert (total, _ids(rows)) == (3, [4, 3, 2])

    async def test_until_inclusive(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, until=_ts(1))
        assert (total, _ids(rows)) == (2, [2, 1])

    async def test_closed_interval(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, since=_ts(1), until=_ts(2))
        assert (total, _ids(rows)) == (3, [4, 3, 2])

    async def test_millisecond_precision_bound(self, db):
        """毫秒级 since（路由 ms_to_datetime 产物）精确截断。"""
        just_before_r2 = _ts(1) - timedelta(milliseconds=1)
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, since=just_before_r2)
        assert total == 3
        at_r2 = _ts(1)
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, since=at_r2)
        assert total == 3  # 含边界：r2（=_ts(1)）命中

    async def test_naive_since_normalized_to_utc(self, db):
        naive = datetime(2026, 9, 10, 11, 0)  # == _ts(1) 的 UTC 裸值
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, since=naive)
        assert total == 3

    async def test_other_tz_since_converted(self, db):
        """+08:00 入参换算回 UTC 再比较（东八区 19:00 == UTC 11:00）。"""
        cst = datetime(2026, 9, 10, 19, 0, tzinfo=timezone(timedelta(hours=8)))
        rows, total = await svc.list_request_logs(db, page=1, page_size=20, since=cst)
        assert total == 3


class TestListPagination:
    async def test_page_slice_and_total(self, db):
        rows, total = await svc.list_request_logs(db, page=2, page_size=2, tenant_id=5)
        assert total == 3
        assert _ids(rows) == [1]  # 第 2 页只剩第 3 条

    async def test_first_page_full_slice(self, db):
        rows, total = await svc.list_request_logs(db, page=1, page_size=2, tenant_id=5)
        assert total == 3
        assert _ids(rows) == [4, 2]

    async def test_beyond_last_page_empty(self, db):
        rows, total = await svc.list_request_logs(db, page=9, page_size=2)
        assert (rows, total) == ([], 4)


class TestGetRequestLog:
    async def test_found_by_request_id(self, db):
        row = await svc.get_request_log(db, request_id="rq-2")
        assert row.id == 2

    async def test_tenant_scope_forces_isolation(self, db):
        """租户域收敛：传 tenant_id 时跨租户视同不存在。"""
        row = await svc.get_request_log(db, request_id="rq-3", tenant_id=6)
        assert row.id == 3
        with pytest.raises(NotFound):
            await svc.get_request_log(db, request_id="rq-3", tenant_id=5)  # r3 属租户 6

    async def test_missing_raises_not_found(self, db):
        with pytest.raises(NotFound):
            await svc.get_request_log(db, request_id="rq-nope")
