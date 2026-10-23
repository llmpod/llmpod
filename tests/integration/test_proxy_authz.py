"""管道授权（AuthZ，第 2 级）集成测试。

规则：model ∈ Key.allowed_models；路由 published；租户 Key 默认放行、被 TenantModelBlock
active 封禁即拒绝、租户冻结拒绝（封禁集/租户状态取配置快照，快照未覆盖回源 PG 点查）；
平台 Key（tenant_id=None）跳过租户封禁。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _fakes import FakeSession

from llmpod.apps.proxyserver.authn import ApiKeyContext
from llmpod.apps.proxyserver.authz import AuthorizationError, authorize
from llmpod.apps.proxyserver.snapshot import RouteEntry, build_config_snapshot
from llmpod.models import Tenant, TenantModelBlock


def _ctx(tenant_id=None, allowed=("m1",)) -> ApiKeyContext:
    return ApiKeyContext(
        key_id=1,
        tenant_id=tenant_id,
        allowed_models=frozenset(allowed),
        rpm_limit=None,
        tpm_limit=None,
        total_token_quota=None,
        model_token_quotas={},
    )


def _entry(status="published", route_id=7) -> RouteEntry:
    return RouteEntry(
        route_id=route_id,
        name="m1",
        status=status,
        input_price_per_mtok=None,
        output_price_per_mtok=None,
        targets=(),
    )


async def _authorize(db: FakeSession, ctx: ApiKeyContext, entry: RouteEntry) -> None:
    """按生产形态组 runtime（快照由预置行装配）后走 authorize。"""
    runtime = SimpleNamespace(snapshot=await build_config_snapshot(db))
    await authorize(runtime, db, ctx, entry)


class TestAuthorize:
    async def test_platform_key_allowed_model_passes(self):
        db = FakeSession()
        await _authorize(db, _ctx(tenant_id=None), _entry())  # 不抛即通过

    async def test_empty_allowed_models_means_all(self):
        """空白名单 = 全部模型可用（跳过白名单校验，而非拒绝一切）。"""
        db = FakeSession()
        await _authorize(db, _ctx(tenant_id=None, allowed=()), _entry())  # 不抛即通过

    async def test_model_not_in_allowed_models_rejected(self):
        with pytest.raises(AuthorizationError):
            await _authorize(FakeSession(), _ctx(allowed=("other",)), _entry())

    async def test_unpublished_route_rejected(self):
        with pytest.raises(AuthorizationError):
            await _authorize(FakeSession(), _ctx(), _entry(status="unpublished"))

    async def test_tenant_key_without_block_passes(self):
        db = FakeSession()
        db.preload(Tenant(id=3, name="t", status="active"))
        await _authorize(db, _ctx(tenant_id=3), _entry())  # 不抛即通过

    async def test_tenant_key_with_active_block_rejected(self):
        db = FakeSession()
        db.preload(Tenant(id=3, name="t", status="active"))
        db.preload(TenantModelBlock(id=1, tenant_id=3, route_id=7, status="active"))
        with pytest.raises(AuthorizationError):
            await _authorize(db, _ctx(tenant_id=3), _entry())

    async def test_tenant_key_disabled_block_passes(self):
        db = FakeSession()
        db.preload(Tenant(id=3, name="t", status="active"))
        db.preload(TenantModelBlock(id=1, tenant_id=3, route_id=7, status="disabled"))
        await _authorize(db, _ctx(tenant_id=3), _entry())  # disabled 不封禁，不抛即通过

    async def test_frozen_tenant_rejected(self):
        db = FakeSession()
        db.preload(Tenant(id=3, name="t", status="disabled"))
        with pytest.raises(AuthorizationError):
            await _authorize(db, _ctx(tenant_id=3), _entry())
