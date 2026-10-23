"""unpublish_deployment_routes 集成测试（FakeSession + 真实 service 逻辑）。

锁定 stop 联动下架语义：本部署唯一来源的 auto 路由 → unpublished；仍被其它端点绑定的
路由（共享/其它部署服务）→ 不动；manual 路由 → 不动；无端点 → no-op。
"""

from __future__ import annotations

from _fakes import FakeSession

from llmpod.models import ModelEndpoint, Route, RouteBinding
from llmpod.services.deployment_service import unpublish_deployment_routes


def _endpoint(session, id: int, deployment_id: str):
    return session.preload(
        ModelEndpoint(
            id=id,
            deployment_id=deployment_id,
            instance_id=None,
            engine_type="vllm",
            protocol="openai",
            api_base="http://x:8000/v1",
            served_model="m1",
            name="m1",
        )
    )


class TestUnpublishDeploymentRoutes:
    async def test_unpublishes_unique_auto_route(self):
        session = FakeSession()
        _endpoint(session, 10, "dep-1")
        session.preload(Route(id=1, name="m1", status="published", origin="auto"))
        session.preload(RouteBinding(id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True))

        await unpublish_deployment_routes(session, "dep-1")

        route = await session.get(Route, 1)
        assert route.status == "unpublished"

    async def test_keeps_route_bound_by_other_endpoint(self):
        session = FakeSession()
        _endpoint(session, 10, "dep-1")
        _endpoint(session, 20, "dep-2")  # 其它部署仍服务该路由
        session.preload(Route(id=1, name="m1", status="published", origin="auto"))
        session.preload(RouteBinding(id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True))
        session.preload(RouteBinding(id=2, route_id=1, endpoint_id=20, weight=1, priority=0, enabled=True))

        await unpublish_deployment_routes(session, "dep-1")

        route = await session.get(Route, 1)
        assert route.status == "published"  # 仍被端点 20 服务，不下架

    async def test_keeps_manual_route(self):
        session = FakeSession()
        _endpoint(session, 10, "dep-1")
        session.preload(Route(id=1, name="m1", status="published", origin="manual"))
        session.preload(RouteBinding(id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True))

        await unpublish_deployment_routes(session, "dep-1")

        route = await session.get(Route, 1)
        assert route.status == "published"  # manual 路由不随部署下架

    async def test_no_endpoints_noop(self):
        session = FakeSession()

        await unpublish_deployment_routes(session, "dep-1")  # 无端点 → no-op
