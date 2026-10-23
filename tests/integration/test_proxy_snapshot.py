"""ProxyServer 配置快照构建器集成测试（FakeSession + 真实构建逻辑）。

锁定装配规则：
- 路由候选 = RouteBinding（enabled）→ ModelEndpoint（enabled + healthy）显式展开，无自动收拢；
- RouteTarget 带 weight/priority（来自 binding）+ protocol/api_key（来自 endpoint）；
- 非 healthy / disabled 端点或 disabled 成员不进快照；下架路由仍在快照（路由层据此 404）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from _fakes import FakeSession

from llmpod.apps.proxyserver.snapshot import build_config_snapshot, load_route_entry
from llmpod.models import ModelEndpoint, Route, RouteBinding

_NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _route(
    id=1, name="qwen3-8b", status="published", cached_price=None, failover_enabled=False, failover_max_retries=0
) -> Route:
    return Route(
        id=id,
        name=name,
        status=status,
        input_price_per_mtok=None,
        output_price_per_mtok=None,
        cached_input_price_per_mtok=cached_price,
        failover_enabled=failover_enabled,
        failover_max_retries=failover_max_retries,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _binding(id=1, route_id=1, endpoint_id=10, weight=1, priority=0, enabled=True) -> RouteBinding:
    return RouteBinding(
        id=id,
        route_id=route_id,
        endpoint_id=endpoint_id,
        weight=weight,
        priority=priority,
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _endpoint(
    id=10, protocol="openai", api_key=None, health="healthy", enabled=True, served_model="Qwen3-8B"
) -> ModelEndpoint:
    return ModelEndpoint(
        id=id,
        deployment_id=None,
        instance_id=None,
        engine_type="vllm",
        protocol=protocol,
        api_base=f"http://10.0.0.{id}:8000",
        path="/v1/chat/completions",
        api_key=api_key,
        served_model=served_model,
        name=f"ep{id}",
        context_window=None,
        operations=["generate"],
        capabilities=[],
        enabled=enabled,
        health_status=health,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _seed(db, *, route=None, bindings=(), endpoints=()):
    if route is not None:
        db.preload(route)
    for b in bindings:
        db.preload(b)
    for e in endpoints:
        db.preload(e)


class TestBuildConfigSnapshot:
    async def test_binding_yields_healthy_endpoint(self):
        db = FakeSession()
        _seed(db, route=_route(), bindings=[_binding(weight=3, priority=2)], endpoints=[_endpoint()])

        snap = await build_config_snapshot(db)

        entry = snap.routes["qwen3-8b"]
        assert entry.status == "published"
        assert [t.endpoint_id for t in entry.targets] == [10]
        t = entry.targets[0]
        assert t.weight == 3 and t.priority == 2
        assert t.protocol == "openai" and t.api_key is None

    async def test_unhealthy_endpoint_excluded(self):
        db = FakeSession()
        _seed(db, route=_route(), bindings=[_binding()], endpoints=[_endpoint(health="down")])

        snap = await build_config_snapshot(db)
        assert snap.routes["qwen3-8b"].targets == ()

    async def test_disabled_endpoint_excluded(self):
        db = FakeSession()
        _seed(db, route=_route(), bindings=[_binding()], endpoints=[_endpoint(enabled=False)])

        snap = await build_config_snapshot(db)
        assert snap.routes["qwen3-8b"].targets == ()

    async def test_disabled_binding_excluded(self):
        db = FakeSession()
        _seed(db, route=_route(), bindings=[_binding(enabled=False)], endpoints=[_endpoint()])

        snap = await build_config_snapshot(db)
        assert snap.routes["qwen3-8b"].targets == ()

    async def test_unpublished_route_included_with_status(self):
        db = FakeSession()
        _seed(db, route=_route(status="unpublished"), bindings=[_binding()], endpoints=[_endpoint()])

        snap = await build_config_snapshot(db)
        assert snap.routes["qwen3-8b"].status == "unpublished"

    async def test_api_key_flows_into_target(self):
        db = FakeSession()
        _seed(db, route=_route(), bindings=[_binding()], endpoints=[_endpoint(api_key="sk-ext")])

        snap = await build_config_snapshot(db)
        assert snap.routes["qwen3-8b"].targets[0].api_key == "sk-ext"

    async def test_cached_input_price_flows_into_entry(self):
        db = FakeSession()
        _seed(db, route=_route(cached_price=Decimal("0.1")), bindings=[_binding()], endpoints=[_endpoint()])

        snap = await build_config_snapshot(db)
        entry = snap.routes["qwen3-8b"]
        assert entry.cached_input_price_per_mtok == Decimal("0.1")
        assert entry.input_price_per_mtok is None

    async def test_failover_flags_flow_into_entry(self):
        db = FakeSession()
        _seed(
            db,
            route=_route(failover_enabled=True, failover_max_retries=2),
            bindings=[_binding()],
            endpoints=[_endpoint()],
        )

        entry = (await build_config_snapshot(db)).routes["qwen3-8b"]
        assert entry.failover_enabled is True and entry.failover_max_retries == 2

    async def test_upstream_name_comes_from_endpoint_name_not_provider(self):
        """上游展示名取端点 name（本地=部署名、外部=登记名），不取可选的 provider_name。

        这是「上游认不出来」的回归锁：该值过去快照自 ``ModelEndpoint.provider_name``，
        而它是选填的——未填供应商的外部端点会得到 None，调用日志只能退化成
        「外部供应商」四个字、大屏明细更是因 SQL 过滤整行消失。两者取不同值，确保取的是 name。
        """
        db = FakeSession()
        ep = _endpoint()
        ep.name = "external-100"
        ep.provider_name = "OpenAI"
        _seed(db, route=_route(), bindings=[_binding()], endpoints=[ep])

        assert (await build_config_snapshot(db)).routes["qwen3-8b"].targets[0].provider_name == "external-100"


class TestLoadRouteEntry:
    """按名回源装配：新增路由/新绑成员不必等轮询周期即可用。

    与全量构建共用装配规则是本类的核心约束——两条路径口径漂移会造出
    「回源查到的和快照里的不是一回事」这类只在发布窗口复现的幽灵问题。
    """

    async def test_matches_snapshot_assembly(self):
        db = FakeSession()
        _seed(
            db,
            route=_route(cached_price=Decimal("0.1"), failover_enabled=True, failover_max_retries=1),
            bindings=[_binding(weight=3, priority=2)],
            endpoints=[_endpoint(api_key="sk-ext")],
        )

        assert await load_route_entry(db, "qwen3-8b") == (await build_config_snapshot(db)).routes["qwen3-8b"]

    async def test_unknown_name_returns_none(self):
        db = FakeSession()
        _seed(db, route=_route(), bindings=[_binding()], endpoints=[_endpoint()])

        assert await load_route_entry(db, "ghost") is None

    async def test_route_without_bindings_has_no_targets(self):
        db = FakeSession()
        _seed(db, route=_route())

        entry = await load_route_entry(db, "qwen3-8b")
        assert entry is not None and entry.targets == ()

    async def test_filtering_matches_snapshot(self):
        """成员 enabled → 端点 enabled + healthy 两级过滤回源同样生效。"""
        db = FakeSession()
        _seed(
            db,
            route=_route(),
            bindings=[
                _binding(id=1, endpoint_id=10),
                _binding(id=2, endpoint_id=11),
                _binding(id=3, endpoint_id=12, enabled=False),
            ],
            endpoints=[_endpoint(id=10), _endpoint(id=11, health="down"), _endpoint(id=12)],
        )

        entry = await load_route_entry(db, "qwen3-8b")
        assert [t.endpoint_id for t in entry.targets] == [10]
