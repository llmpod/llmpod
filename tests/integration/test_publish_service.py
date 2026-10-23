"""route 管理 service 集成测试（FakeSession + 真实 service 逻辑）。

锁定：部署 READY 自动发布 ensure_route_and_binding 幂等确保 路由(published) + 成员(weight=1)；
已存在的路由/成员不被自动发布翻动；管理端手动建路由（重名 Conflict、可携带初始成员）；
路由改名冲突；上下架/定价/failover/成员权重管理端语义；成员调整 NotFound 语义；删路由清引用。
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from _fakes import TRI_STATE_ROUTE_FIELDS, FakeSession
from llmpod.core.exceptions import Conflict, NotFound
from llmpod.models import Route, RouteBinding, TenantModelBlock
from llmpod.services.publish_service import (
    UNSET,
    create_route,
    delete_binding,
    delete_route,
    ensure_route,
    ensure_route_and_binding,
    list_bindings,
    list_routes,
    rename_route,
    republish_route,
    unpublish_route,
    update_binding,
    update_route,
    upsert_binding,
)


class TestCreateRoute:
    async def test_create_route_published_by_default(self):
        db = FakeSession()

        route = await create_route(db, name="qwen3-8b")

        assert route.name == "qwen3-8b"
        assert route.status == "published"
        # 默认开 + 重试 2 次（与 ORM 列默认 / 创建请求模型三处一致）
        assert route.failover_enabled is True and route.failover_max_retries == 2

    async def test_create_route_with_pricing_and_failover(self):
        from decimal import Decimal

        db = FakeSession()

        route = await create_route(
            db,
            name="m1",
            input_price_per_mtok=Decimal("1.5"),
            output_price_per_mtok=Decimal("6"),
            cached_input_price_per_mtok=Decimal("0.15"),
            failover_enabled=True,
            failover_max_retries=2,
        )

        assert route.input_price_per_mtok == Decimal("1.5")
        assert route.output_price_per_mtok == Decimal("6")
        assert route.cached_input_price_per_mtok == Decimal("0.15")
        assert route.failover_enabled is True and route.failover_max_retries == 2

    async def test_create_route_duplicate_name_conflicts(self):
        db = FakeSession()
        await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        with pytest.raises(Conflict):
            await create_route(db, name="m1")

    async def test_create_route_with_initial_bindings(self):
        from types import SimpleNamespace

        db = FakeSession()

        route = await create_route(
            db,
            name="m1",
            bindings=[
                SimpleNamespace(endpoint_id=11, weight=3, priority=2),
                SimpleNamespace(endpoint_id=22, weight=None, priority=None),
            ],
        )

        bindings = db.all(RouteBinding)
        assert len(bindings) == 2
        by_endpoint = {b.endpoint_id: b for b in bindings}
        assert by_endpoint[11].route_id == route.id
        assert (by_endpoint[11].weight, by_endpoint[11].priority) == (3, 2)
        assert by_endpoint[11].enabled is True
        # 缺省 weight=1 / priority=0
        assert (by_endpoint[22].weight, by_endpoint[22].priority) == (1, 0)
        assert by_endpoint[22].enabled is True

    async def test_create_route_without_bindings_adds_none(self):
        db = FakeSession()

        route = await create_route(db, name="m1")

        assert db.all(RouteBinding) == []
        assert await list_bindings(db, route_id=route.id) == []


class TestEnsureRouteAndBinding:
    async def test_creates_route_and_binding(self):
        db = FakeSession()

        route, binding = await ensure_route_and_binding(db, name="Qwen3-8B", endpoint_id=11)

        assert route.name == "Qwen3-8B"
        assert route.status == "published"
        assert binding.route_id == route.id
        assert binding.endpoint_id == 11
        assert binding.weight == 1 and binding.priority == 0 and binding.enabled is True

    async def test_idempotent_recall_keeps_existing(self):
        db = FakeSession()
        route, binding = await ensure_route_and_binding(db, name="m1", endpoint_id=11)
        route.status = "unpublished"  # 管理员下架
        binding.weight = 5  # 管理员调过权重

        route2, binding2 = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        assert route2 is route and route2.status == "unpublished"
        assert binding2 is binding and binding2.weight == 5
        assert len(db.all(Route)) == 1 and len(db.all(RouteBinding)) == 1

    async def test_recreates_binding_if_missing(self):
        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)
        await db.delete(db.all(RouteBinding)[0])

        _, binding2 = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        assert binding2.route_id == route.id
        assert len(db.all(RouteBinding)) == 1


class TestEnsureRouteFields:
    async def test_creates_route_with_publish_fields(self):
        from decimal import Decimal

        db = FakeSession()

        route = await ensure_route(
            db,
            name="qwen3-8b",
            model_type="llm",
            description="desc",
            tags=["思考"],
            input_price_per_mtok=1.5,
            output_price_per_mtok=6.0,
            cached_input_price_per_mtok=0.15,
        )

        assert route.name == "qwen3-8b"
        assert route.status == "published"
        assert route.origin == "auto"
        assert route.model_type == "llm"
        assert route.description == "desc"
        assert route.tags == ["思考"]
        assert route.input_price_per_mtok == Decimal("1.5")
        assert route.output_price_per_mtok == Decimal("6.0")
        assert route.cached_input_price_per_mtok == Decimal("0.15")

    async def test_existing_route_not_overwritten(self):
        from decimal import Decimal

        db = FakeSession()
        route = await ensure_route(db, name="m1")
        route.status = "unpublished"  # 管理员下架
        route.tags = ["视觉"]  # 管理员改过标签
        route.input_price_per_mtok = Decimal("9.9")  # 管理员定过价

        route2 = await ensure_route(db, name="m1", tags=["思考"], input_price_per_mtok=1.0)

        assert route2 is route
        assert route2.status == "unpublished"  # 不翻动管理员下架
        assert route2.tags == ["视觉"]  # 不覆盖管理员标签
        assert route2.input_price_per_mtok == Decimal("9.9")  # 不覆盖定价

    async def test_unknown_tag_raises(self):
        from llmpod.core.exceptions import LLMPodValidationError

        with pytest.raises(LLMPodValidationError):
            await ensure_route(FakeSession(), name="m1", tags=["不存在的标签"])


#: 建路由时写满的三态列初始值（键即 TRI_STATE_ROUTE_FIELDS），「传 UNSET = 不改」断言以此为基准
_INITIAL_TRI_STATE_FIELDS = {
    "context_length": 8192,
    "input_price_per_mtok": Decimal("1.5"),
    "output_price_per_mtok": Decimal("6"),
    "cached_input_price_per_mtok": Decimal("0.15"),
    "failover_max_retries": 5,
}

#: 显式 null 与众不同的处理：其余三态列落 NULL（None），非空列 failover_max_retries 落 0（不切换）
_TRI_STATE_CLEARED = {"failover_max_retries": 0}


async def _route_with_tri_state_fields(db: FakeSession) -> Route:
    return await create_route(db, name="m1", **_INITIAL_TRI_STATE_FIELDS)


class TestRouteLifecycle:
    async def test_unpublish_and_republish(self):
        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        assert (await unpublish_route(db, route_id=route.id)).status == "unpublished"
        assert (await republish_route(db, route_id=route.id)).status == "published"

    async def test_unpublish_missing_raises_not_found(self):
        with pytest.raises(NotFound):
            await unpublish_route(FakeSession(), route_id=99)

    async def test_rename_route(self):
        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        renamed = await rename_route(db, route_id=route.id, new_name="qwen3-8b")
        assert renamed.name == "qwen3-8b"

    async def test_rename_to_existing_name_conflicts(self):
        db = FakeSession()
        await ensure_route_and_binding(db, name="m1", endpoint_id=11)
        await ensure_route_and_binding(db, name="m2", endpoint_id=12)

        m1 = next(r for r in db.all(Route) if r.name == "m1")
        with pytest.raises(Conflict):
            await rename_route(db, route_id=m1.id, new_name="m2")

    async def test_update_route_sets_pricing_and_failover(self):
        from decimal import Decimal

        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        updated = await update_route(
            db,
            route_id=route.id,
            input_price_per_mtok=Decimal("1.5"),
            output_price_per_mtok=Decimal("6"),
            cached_input_price_per_mtok=Decimal("0.15"),
            failover_enabled=True,
            failover_max_retries=2,
        )
        assert updated.input_price_per_mtok == Decimal("1.5")
        assert updated.output_price_per_mtok == Decimal("6")
        assert updated.cached_input_price_per_mtok == Decimal("0.15")
        assert updated.failover_enabled is True and updated.failover_max_retries == 2

    async def test_update_route_none_keeps_not_null_fields(self):
        """不经哨兵的列：传 None 仍=不改（null 无处可落，或清空信号本就是空串 / 空数组）。"""
        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        updated = await update_route(
            db,
            route_id=route.id,
            name=None,
            model_type=None,
            tags=None,
            failover_enabled=None,
        )
        assert updated.name == "m1"
        assert updated.model_type == "llm"
        assert updated.failover_enabled is True and updated.failover_max_retries == 2

    async def test_update_route_tri_state_defaults_are_unset(self):
        """哨兵标注即三态名单：改回 None 会让「不传」静默变成「清空」，漏标则名单漏人。"""
        params = inspect.signature(update_route).parameters
        annotated = {n for n, p in params.items() if "_UnsetType" in str(p.annotation)}
        assert annotated == set(TRI_STATE_ROUTE_FIELDS), "标注 _UnsetType 的参数应与三态名单一致"
        for field in TRI_STATE_ROUTE_FIELDS:
            assert params[field].default is UNSET, f"{field} 默认值应为 UNSET（未传=不改）"
        for field in ("name", "model_type", "description", "tags", "failover_enabled"):
            assert params[field].default is None, f"{field} 不经哨兵（NOT NULL 列，或清空信号是空串）"

    @pytest.mark.parametrize("field", TRI_STATE_ROUTE_FIELDS)
    async def test_update_route_unset_keeps_field(self, field):
        """不传该参数（UNSET）= 不改：更新别的字段不动它（非空的三态列同样不得被静默置 0）。"""
        db = FakeSession()
        route = await _route_with_tri_state_fields(db)

        updated = await update_route(db, route_id=route.id, description="改个描述")

        assert getattr(updated, field) == _INITIAL_TRI_STATE_FIELDS[field]  # 缺字段在此 KeyError，与名单同锁

    @pytest.mark.parametrize("field", TRI_STATE_ROUTE_FIELDS)
    async def test_update_route_explicit_none_clears_field(self, field):
        """显式传 None = 「清空」：结果由列是否可空决定（``_TRI_STATE_CLEARED``），兄弟字段一律不牵连。

        修好之前全被 `is not None` 守卫吞掉，本用例即报旧值。
        """
        db = FakeSession()
        route = await _route_with_tri_state_fields(db)

        updated = await update_route(db, route_id=route.id, **{field: None})

        assert {f: getattr(updated, f) for f in TRI_STATE_ROUTE_FIELDS} == {
            **_INITIAL_TRI_STATE_FIELDS,
            field: _TRI_STATE_CLEARED.get(field),
        }

    async def test_update_route_description_clears_via_empty_string(self):
        """锁定 description 的既有清空约定（空串，非 null）：它不经哨兵，需要有测试兜住。"""
        db = FakeSession()
        route = await create_route(db, name="m1", description="原描述")

        updated = await update_route(db, route_id=route.id, description="")

        assert updated.description == ""


class TestBindings:
    async def test_upsert_binding_updates_weight(self):
        db = FakeSession()
        route, binding = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        updated = await upsert_binding(db, route_id=route.id, endpoint_id=11, weight=3, priority=2)
        assert updated is binding
        assert (updated.weight, updated.priority) == (3, 2)

    async def test_upsert_binding_creates_if_missing(self):
        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        binding = await upsert_binding(db, route_id=route.id, endpoint_id=22, weight=2)
        assert binding.endpoint_id == 22 and binding.weight == 2
        assert len(await list_bindings(db, route_id=route.id)) == 2

    async def test_update_binding_adjusts_weight_and_priority(self):
        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        updated = await update_binding(db, route_id=route.id, endpoint_id=11, weight=5, priority=3)
        assert (updated.weight, updated.priority) == (5, 3)

    async def test_update_binding_missing_raises_not_found(self):
        db = FakeSession()
        await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        with pytest.raises(NotFound):
            await update_binding(db, route_id=1, endpoint_id=99, weight=5)

    async def test_disable_and_enable_binding(self):
        db = FakeSession()
        route, binding = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        disabled = await update_binding(db, route_id=route.id, endpoint_id=11, enabled=False)
        assert disabled is binding and disabled.enabled is False

        enabled = await update_binding(db, route_id=route.id, endpoint_id=11, enabled=True)
        assert enabled is binding and enabled.enabled is True

    async def test_delete_binding(self):
        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)

        await delete_binding(db, route_id=route.id, endpoint_id=11)
        assert await list_bindings(db, route_id=route.id) == []


class TestDeleteRoute:
    async def test_delete_route_clears_bindings_and_blocks(self):
        db = FakeSession()
        route, _ = await ensure_route_and_binding(db, name="m1", endpoint_id=11)
        await upsert_binding(db, route_id=route.id, endpoint_id=22)
        db.preload(TenantModelBlock(id=1, tenant_id=3, route_id=route.id, status="active"))

        await delete_route(db, route_id=route.id)

        assert db.all(Route) == []
        assert db.all(RouteBinding) == []
        assert db.all(TenantModelBlock) == []

    async def test_delete_route_idempotent_on_missing(self):
        with pytest.raises(NotFound):
            await delete_route(FakeSession(), route_id=99)


class TestListRoutes:
    async def test_returns_routes(self):
        db = FakeSession()
        await ensure_route_and_binding(db, name="m1", endpoint_id=11)
        await ensure_route_and_binding(db, name="m2", endpoint_id=12)

        assert len(await list_routes(db)) == 2
