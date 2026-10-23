"""api_key_service.list_api_keys 单测：列表过滤（租户作用域 / 平台直发开关 / 状态）。

admin 路由不传 tenant_id 时置位 ``exclude_tenant_scope``，只看平台直发 Key
（``tenant_id IS NULL``）；显式传 tenant_id 精确看该租户；不传开关（租户端用法）
行为不变。``name`` 模糊过滤走 SQL ``ilike``，MiniDB 桩不支持，故本文件不覆盖该分支。
"""

from __future__ import annotations

import pytest

from llmpod.models import ApiKey
from llmpod.services import api_key_service as svc
from tests.unit._minidb import MiniDB


def _key(id: int, *, tenant_id: int | None, status: str = "active", name: str | None = None) -> ApiKey:
    return ApiKey(
        id=id,
        key_hash=f"hash-{id}".ljust(64, "0"),
        key_plaintext_encrypted="",
        tenant_id=tenant_id,
        name=name or f"key-{id}",
        status=status,
    )


def _ids(rows: list[ApiKey]) -> list[int]:
    return [r.id for r in rows]


@pytest.fixture
def db() -> MiniDB:
    """2 行平台直发（tenant_id=None）+ 2 行租户 Key（5 / 6）+ 1 行历史残留 0。"""
    d = MiniDB()
    d.preload(
        _key(1, tenant_id=None, name="platform-a"),
        _key(2, tenant_id=5, name="tenant-5"),
        _key(3, tenant_id=None, status="disabled", name="platform-b"),
        _key(4, tenant_id=6, name="tenant-6"),
        _key(5, tenant_id=0, name="legacy-zero"),
    )
    return d


class TestListPlatformScope:
    """admin 视角开关：exclude_tenant_scope 在 tenant_id 缺省/传 0 时看平台域（NULL ∪ 0）。"""

    async def test_platform_scope_returns_only_null_tenant(self, db):
        rows = await svc.list_api_keys(db, tenant_id=None, exclude_tenant_scope=True)
        assert _ids(rows) == [5, 3, 1]  # 平台域行（含历史 0），id 倒序

    async def test_platform_scope_on_zero_tenant_id_same_domain(self, db):
        """传 0 = 平台域（与审计日志同口径），不是「精确等值 0」。"""
        rows = await svc.list_api_keys(db, tenant_id=0, exclude_tenant_scope=True)
        assert _ids(rows) == [5, 3, 1]

    async def test_platform_scope_combines_with_status_filter(self, db):
        rows = await svc.list_api_keys(db, tenant_id=None, status="active", exclude_tenant_scope=True)
        assert _ids(rows) == [5, 1]

    async def test_tenant_filter_wins_over_scope_switch(self, db):
        """显式传 tenant_id 时精确看该租户，不产生 IS NULL 限定。"""
        rows = await svc.list_api_keys(db, tenant_id=5, exclude_tenant_scope=True)
        assert _ids(rows) == [2]

    async def test_tenant_route_default_keeps_all(self, db):
        """不传开关（租户端用法）：不按租户过滤，与改动前一致。"""
        rows = await svc.list_api_keys(db, tenant_id=None)
        assert _ids(rows) == [5, 4, 3, 2, 1]

    async def test_tenant_scope_still_filters_when_switch_off(self, db):
        rows = await svc.list_api_keys(db, tenant_id=6)
        assert _ids(rows) == [4]
