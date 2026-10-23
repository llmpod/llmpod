"""model_block_service 集成测试：租户模型封禁的全量替换语义（PUT desired set）+ 批量读装配。"""

from __future__ import annotations

import pytest
from _fakes import FakeSession

from llmpod.models import Route, Tenant, TenantModelBlock
from llmpod.services.model_block_service import get_blocked_models_map, set_tenant_model_blocks


def _tenant(id=3) -> Tenant:
    return Tenant(id=id, name="t3", status="active")


class TestSetTenantModelBlocks:
    async def test_replaces_set_idempotently(self):
        db = FakeSession()
        db.preload(_tenant())

        blocks = await set_tenant_model_blocks(db, tenant_id=3, route_ids=[7, 8])
        assert sorted(b.route_id for b in blocks) == [7, 8]
        assert all(b.status == "active" for b in blocks)

        # 再次设置 [8, 9]：7 被移除、9 新增、8 保留同一行
        blocks2 = await set_tenant_model_blocks(db, tenant_id=3, route_ids=[8, 9])
        assert sorted(b.route_id for b in blocks2) == [8, 9]
        all_rows = db.all(TenantModelBlock)
        assert sorted(b.route_id for b in all_rows) == [8, 9]

    async def test_existing_disabled_block_reactivated(self):
        db = FakeSession()
        db.preload(_tenant())
        db.preload(TenantModelBlock(tenant_id=3, route_id=7, status="disabled"))

        blocks = await set_tenant_model_blocks(db, tenant_id=3, route_ids=[7])
        assert blocks[0].status == "active"

    async def test_unknown_tenant_rejected(self):
        with pytest.raises(Exception):
            await set_tenant_model_blocks(FakeSession(), tenant_id=99, route_ids=[7])


class TestGetBlockedModelsMap:
    async def test_maps_resolved_models_per_tenant(self):
        db = FakeSession()
        db.preload(Route(id=7, name="gpt-4o", model_type="llm"))
        db.preload(Route(id=9, name="bge-m3", model_type="embedding"))
        db.preload(TenantModelBlock(id=1, tenant_id=3, route_id=7, status="active"))
        db.preload(TenantModelBlock(id=2, tenant_id=3, route_id=8, status="disabled"))  # disabled 不封禁
        db.preload(TenantModelBlock(id=3, tenant_id=4, route_id=9, status="active"))  # 别的租户

        m = await get_blocked_models_map(db, [3, 4, 5])
        assert [b.route_id for b in m[3]] == [7]
        assert m[3][0].name == "gpt-4o"
        assert m[3][0].model_type == "llm"
        assert [b.name for b in m[4]] == ["bge-m3"]
        assert 5 not in m  # 5 无封禁，不出现在结果中

    async def test_empty_ids_returns_empty(self):
        assert await get_blocked_models_map(FakeSession(), []) == {}
