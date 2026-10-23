"""endpoint_service 集成测试（FakeSession + 真实 service 逻辑）。

锁定：外部端点登记（deployment_id=NULL、engine_type=external）、维护、删除时解绑路由成员。
"""

from __future__ import annotations

import pytest
from _fakes import FakeSession

from llmpod.core.exceptions import NotFound
from llmpod.models import ModelEndpoint, Route, RouteBinding
from llmpod.services.endpoint_service import (
    create_endpoint,
    delete_endpoint,
    list_endpoints,
    update_endpoint,
)
from llmpod.services.publish_service import ensure_route_and_binding


class TestCreateEndpoint:
    async def test_creates_external_endpoint(self):
        db = FakeSession()

        ep = await create_endpoint(
            db,
            protocol="openai",
            api_base="https://api.openai.com/v1",
            served_model="gpt-4o",
            name="OpenAI-gpt4o",
            api_key="sk-x",
            provider_name="OpenAI",
        )

        assert ep.deployment_id is None
        assert ep.engine_type == "external"
        assert ep.protocol == "openai"
        assert ep.enabled is True and ep.health_status == "healthy"
        assert len(db.all(ModelEndpoint)) == 1

    async def test_update_and_delete(self):
        db = FakeSession()
        ep = await create_endpoint(db, protocol="openai", api_base="http://a/v1", served_model="m", name="n")

        updated = await update_endpoint(db, endpoint_id=ep.id, api_base="http://b/v1", enabled=False)
        assert updated.api_base == "http://b/v1" and updated.enabled is False

        await delete_endpoint(db, endpoint_id=ep.id)
        assert db.all(ModelEndpoint) == []

    async def test_delete_unbinds_route_members(self):
        db = FakeSession()
        ep = await create_endpoint(db, protocol="openai", api_base="http://a/v1", served_model="m", name="n")
        route, binding = await ensure_route_and_binding(db, name="m", endpoint_id=ep.id)

        await delete_endpoint(db, endpoint_id=ep.id)

        assert db.all(ModelEndpoint) == []
        assert db.all(RouteBinding) == []
        assert len(db.all(Route)) == 1  # 路由保留

    async def test_list_endpoints(self):
        db = FakeSession()
        await create_endpoint(db, protocol="openai", api_base="http://a/v1", served_model="m1", name="a")
        await create_endpoint(db, protocol="anthropic", api_base="http://b", served_model="m2", name="b")

        assert len(await list_endpoints(db)) == 2

    async def test_get_missing_raises_not_found(self):
        with pytest.raises(NotFound):
            await update_endpoint(FakeSession(), endpoint_id=99, enabled=False)
