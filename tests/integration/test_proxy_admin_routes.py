"""API 网关配置态 admin 路由契约测试（stub 驱动，遵循项目 HTTP 层测试原则）。

覆盖：api-keys 签发/列表/禁用/启用/删除、publishes 列表/创建/上下架/路由调整。
只验路由注册、依赖链（鉴权 401 / require_permission）、
统一 Result 契约、领域异常→HTTP 映射；业务逻辑由 service 测试覆盖。
全部端点仅 GET/POST。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from _fakes import (
    CLEARABLE_KEY_FIELDS,
    KEY_QUOTA_FIELDS,
    KEY_QUOTA_MAX_CASES,
    NUMERIC_OVERSIZED,
    TRI_STATE_ROUTE_FIELDS,
    admin_session,
)

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.core.exceptions import NotFound
from llmpod.core.secrets import encrypt
from llmpod.db import get_redis
from llmpod.models import ApiKey, Route, RouteBinding
from llmpod.services import publish_service
from llmpod.services.api_key_service import UNSET


def _stub_key(id=1, **ovr) -> ApiKey:
    base = dict(
        id=id,
        key_hash="a" * 64,
        tenant_id=None,
        name="k",
        allowed_models=["m1"],
        rpm_limit=None,
        tpm_limit=None,
        total_token_quota=None,
        model_token_quotas={},
        budget_limit=None,
        budget_spent=Decimal("0"),
        budget_frozen=Decimal("0"),
        status="active",
        expires_at=None,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return ApiKey(**base)


def _stub_route(id=1, **ovr) -> Route:
    base = dict(
        id=id,
        name="m1",
        status="published",
        origin="manual",
        model_type="llm",
        description=None,
        input_price_per_mtok=None,
        output_price_per_mtok=None,
        cached_input_price_per_mtok=None,
        failover_enabled=False,
        failover_max_retries=0,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return Route(**base)


def _stub_binding(id=1, **ovr) -> RouteBinding:
    base = dict(
        id=id,
        route_id=1,
        endpoint_id=10,
        weight=1,
        priority=0,
        enabled=True,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return RouteBinding(**base)


class _FakeRedis:
    async def delete(self, *keys):
        return len(keys)


@pytest.fixture
def stub_redis():
    async def _gen():
        yield _FakeRedis()

    app.dependency_overrides[get_redis] = _gen
    yield
    app.dependency_overrides.pop(get_redis, None)


class TestApiKeysRoutes:
    async def test_routes_require_auth(self, client: AsyncClient):
        for method, path in [
            ("post", "/api/v1/admin/api-keys"),
            ("get", "/api/v1/admin/api-keys"),
            ("post", "/api/v1/admin/api-keys/1/disable"),
            ("post", "/api/v1/admin/api-keys/1/enable"),
            ("post", "/api/v1/admin/api-keys/1/delete"),
            ("post", "/api/v1/admin/api-keys/1/update"),
        ]:
            resp = await client.request(method, path, json={})
            assert resp.status_code == 401, f"{method} {path} 应受登录保护"

    async def test_create_returns_plaintext_once(self, client: AsyncClient, monkeypatch, stub_redis):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        async def _fake_create(db, redis, **kw):
            return _stub_key(id=9), "sk-plaintext-once"

        import llmpod.apps.webserver.routes.admin.api_keys as route_mod

        monkeypatch.setattr(route_mod, "create_api_key", _fake_create)

        resp = await client.post(
            "/api/v1/admin/api-keys",
            json={
                "name": "k",
                "allowed_models": ["m1"],
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert body["data"]["plaintext"] == "sk-plaintext-once"
        assert body["data"]["api_key"]["key_hash"] == "a" * 64  # 哈希可见，明文仅顶层一次

    async def test_delete_not_found_maps_404(self, client: AsyncClient, monkeypatch, stub_redis):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        async def _fake_delete(db, redis, *, key_id):
            raise NotFound("no")

        import llmpod.apps.webserver.routes.admin.api_keys as route_mod

        monkeypatch.setattr(route_mod, "delete_api_key", _fake_delete)

        resp = await client.post("/api/v1/admin/api-keys/99/delete")
        assert resp.status_code == 404

    async def test_update_tri_state_passes_none_for_null_unset_for_omitted(
        self, client: AsyncClient, monkeypatch, stub_redis
    ):
        """三态透传：显式 null → service 收到 None（清空）；键缺省 → UNSET（不改）。

        写入侧行为由 service 测试覆盖，此处只锁「清空信号不得在路由层被吞掉」。
        """
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen: dict = {}

        async def _fake_update(db, redis, **kw):
            seen.update(kw)
            return _stub_key(id=1)

        import llmpod.apps.webserver.routes.admin.api_keys as route_mod

        monkeypatch.setattr(route_mod, "update_api_key", _fake_update)

        resp = await client.post("/api/v1/admin/api-keys/1/update", json={"expires_at": None})
        assert resp.status_code == 200
        assert seen["expires_at"] is None, "显式 null 必须原样传 None（清空），不得被吞成哨兵"
        for field in CLEARABLE_KEY_FIELDS:
            if field != "expires_at":
                assert seen[field] is UNSET, f"{field} 未传应传哨兵（不改）"

        seen.clear()
        resp = await client.post("/api/v1/admin/api-keys/1/update", json={})
        assert resp.status_code == 200
        for field in CLEARABLE_KEY_FIELDS:
            assert seen[field] is UNSET, f"{field} 缺省应传哨兵（不改），否则保存会误清空未提交字段"

    async def test_clear_expires_at_end_to_end(self, client: AsyncClient, stub_redis):
        """走真实 service 的复现用例：清空过期时间后，响应里必须不再是旧值。

        有意偏离「HTTP 层只验契约、业务逻辑交给 service 测试」的约定：本 bug 正长在
        请求体 → 路由 → service 的接缝上（两边各自自洽，合起来丢信号），故此处用真
        service + 内存会话把用户报告的路径整条跑通。改回 ``expires_at=body.expires_at``
        或把 service 守卫退回 ``is not None`` 都会让本用例失败。
        """
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        from _fakes import FakeSession

        stale = datetime(2030, 1, 1, tzinfo=timezone.utc)
        db = FakeSession()
        db.preload(_stub_key(id=1, expires_at=stale))

        import llmpod.db as db_mod

        async def _db_gen():
            yield db

        app.dependency_overrides[db_mod.get_db] = _db_gen

        resp = await client.post("/api/v1/admin/api-keys/1/update", json={"expires_at": None})

        assert resp.status_code == 200
        assert resp.json()["data"]["expires_at"] is None, "响应仍在回显旧过期时间 = 清空被静默吞掉"
        assert db.all(ApiKey)[0].expires_at is None, "库里也必须真的落 NULL"

    @pytest.mark.parametrize("field", KEY_QUOTA_FIELDS)
    async def test_create_rejects_over_range_quota(self, client: AsyncClient, stub_redis, field):
        """超长数字须拦在请求模型（422 + 字段 loc），不得直达 asyncpg 参数绑定。

        422 本身即证明未触达 service：FastAPI 在请求体校验阶段就拒绝，端点函数根本不执行。
        """
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        resp = await client.post("/api/v1/admin/api-keys", json={"name": "k1", field: NUMERIC_OVERSIZED})

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

    @pytest.mark.parametrize("field", KEY_QUOTA_FIELDS)
    async def test_update_rejects_over_range_quota(self, client: AsyncClient, stub_redis, field):
        """同 create：更新路径的同一批字段同样须拦成 422。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        resp = await client.post("/api/v1/admin/api-keys/1/update", json={field: NUMERIC_OVERSIZED})

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

    @pytest.mark.parametrize("field,value", KEY_QUOTA_MAX_CASES)
    async def test_update_accepts_column_max_quota(self, client: AsyncClient, monkeypatch, stub_redis, field, value):
        """上界本身是合法值（= 对应 DB 列的物理上界）：卡在列的容纳范围，不收得过紧。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen: dict = {}

        async def _fake_update(db, redis, **kw):
            seen.update(kw)
            return _stub_key(id=1)

        import llmpod.apps.webserver.routes.admin.api_keys as route_mod

        monkeypatch.setattr(route_mod, "update_api_key", _fake_update)

        resp = await client.post("/api/v1/admin/api-keys/1/update", json={field: value})

        assert resp.status_code == 200
        # 值须原样抵达 service（未被静默取整/截断）；budget_limit 经 Decimal 归一
        assert seen[field] == (Decimal(value) if isinstance(value, str) else value)

    async def test_list_returns_plaintext_and_passes_filters(self, client: AsyncClient, monkeypatch):
        """列表回显完整明文（凭据材料口径），name/status/tenant_id 过滤透传 service。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen = {}

        async def _fake_list(db, *, tenant_id=None, name=None, status=None, exclude_tenant_scope=None):
            seen.update(tenant_id=tenant_id, name=name, status=status, exclude_tenant_scope=exclude_tenant_scope)
            key = _stub_key(id=7, key_plaintext_encrypted=encrypt("sk-abcdefgh1234"))
            return [key]

        import llmpod.apps.webserver.routes.admin.api_keys as route_mod

        monkeypatch.setattr(route_mod, "list_api_keys", _fake_list)

        resp = await client.get(
            "/api/v1/admin/api-keys",
            params={"name": "prod", "status": "active", "tenant_id": 5},
        )

        assert resp.status_code == 200
        item = resp.json()["data"][0]
        assert item["plaintext"] == "sk-abcdefgh1234", "列表必须回显完整明文（可用凭据）"
        assert seen == {"tenant_id": 5, "name": "prod", "status": "active", "exclude_tenant_scope": False}

    async def test_list_defaults_to_platform_scope(self, client: AsyncClient, monkeypatch):
        """admin 默认视角由路由派生：带 tenant_id → 关掉平台域限定；不带 → 只看平台直发 Key。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        seen: list[tuple[int | None, bool | None]] = []

        async def _fake_list(db, *, tenant_id=None, name=None, status=None, exclude_tenant_scope=None):
            seen.append((tenant_id, exclude_tenant_scope))
            return []

        import llmpod.apps.webserver.routes.admin.api_keys as route_mod

        monkeypatch.setattr(route_mod, "list_api_keys", _fake_list)

        # 带 tenant_id → 透传给 service，且关掉平台域限定（精确看该租户）
        resp = await client.get("/api/v1/admin/api-keys", params={"tenant_id": 42})
        assert resp.status_code == 200
        assert seen == [(42, False)]

        # 不带 tenant_id → 平台视角：平台域收敛开关置位（NULL ∪ 0，口径同调用日志/审计）
        resp = await client.get("/api/v1/admin/api-keys")
        assert resp.status_code == 200
        assert seen == [(42, False), (None, True)]

        # 传 0 同样是平台视角（平台域哨兵），不是「精确等值 0」
        resp = await client.get("/api/v1/admin/api-keys", params={"tenant_id": 0})
        assert resp.status_code == 200
        assert seen == [(42, False), (None, True), (0, True)]


#: 路由数值列的物理上界（PG integer / Numeric(12,6)），update 与 create 两条路径共用。
#: 故意写字面量而非引用 schemas/request.py 的 `INT32_MAX`：这里锁的是「上界 = 列能真正存下的
#: 最大值」，引用常量会退化成同义反复，列被拓宽后也照样绿。
_COLUMN_MAX_CASES = (
    ("context_length", 2**31 - 1),
    ("failover_max_retries", 2**31 - 1),
    ("input_price_per_mtok", "999999.999999"),
)


class TestPublishesRoutes:
    async def test_routes_require_auth(self, client: AsyncClient):
        for method, path in [
            ("get", "/api/v1/admin/publishes"),
            ("post", "/api/v1/admin/publishes"),
            ("post", "/api/v1/admin/publishes/1/unpublish"),
            ("post", "/api/v1/admin/publishes/1/republish"),
            ("post", "/api/v1/admin/publishes/1/update"),
            ("post", "/api/v1/admin/publishes/1/delete"),
            ("post", "/api/v1/admin/publishes/1/bindings/10"),
            ("post", "/api/v1/admin/publishes/1/bindings/10/update"),
            ("post", "/api/v1/admin/publishes/1/bindings/10/enable"),
            ("post", "/api/v1/admin/publishes/1/bindings/10/disable"),
            ("post", "/api/v1/admin/publishes/1/bindings/10/delete"),
        ]:
            resp = await client.request(method, path, json={})
            assert resp.status_code == 401, f"{method} {path} 应受登录保护"

    async def test_list_returns_route_with_bindings(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        async def _fake_list(db):
            return [_stub_route(id=1, name="m1"), _stub_route(id=2, name="m2", status="unpublished")]

        async def _fake_bindings_for_routes(db, route_ids):
            return {rid: [_stub_binding(id=1, route_id=rid)] for rid in route_ids}

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "list_routes", _fake_list)
        monkeypatch.setattr(route_mod, "list_bindings_for_routes", _fake_bindings_for_routes)

        # 缺省全集（含下架）：发布管理页要对已下架路由再上架（回显「上架」按钮）
        resp = await client.get("/api/v1/admin/publishes")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert [d["route"]["name"] for d in data] == ["m1", "m2"]
        assert data[0]["bindings"][0]["weight"] == 1

        # ?status=published 只看在线；?status=unpublished 只看下架；?status=all（缺省别名）全集
        resp_published = await client.get("/api/v1/admin/publishes", params={"status": "published"})
        assert [d["route"]["name"] for d in resp_published.json()["data"]] == ["m1"]
        resp_unpublished = await client.get("/api/v1/admin/publishes", params={"status": "unpublished"})
        assert [d["route"]["name"] for d in resp_unpublished.json()["data"]] == ["m2"]
        resp_all = await client.get("/api/v1/admin/publishes", params={"status": "all"})
        assert len(resp_all.json()["data"]) == 2

    async def test_update_route_accepts_cached_price_and_failover(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        def _stored(value):
            """三态：不改（``publish_service.UNSET``）时回显 stub 库里那几列的原值 None。"""
            return None if value is publish_service.UNSET else value

        async def _fake_update(
            db,
            *,
            route_id,
            name=None,
            model_type=None,
            description=None,
            context_length=None,
            tags=None,
            input_price_per_mtok=None,
            output_price_per_mtok=None,
            cached_input_price_per_mtok=None,
            failover_enabled=None,
            failover_max_retries=None,
        ):
            return _stub_route(
                id=route_id,
                name=name or "m1",
                input_price_per_mtok=_stored(input_price_per_mtok),
                output_price_per_mtok=_stored(output_price_per_mtok),
                cached_input_price_per_mtok=_stored(cached_input_price_per_mtok),
                failover_enabled=failover_enabled,
                failover_max_retries=failover_max_retries,
            )

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "update_route", _fake_update)

        resp = await client.post(
            "/api/v1/admin/publishes/1/update",
            json={
                "cached_input_price_per_mtok": "0.15",
                "failover_enabled": True,
                "failover_max_retries": 2,
            },
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert str(data["cached_input_price_per_mtok"]) == "0.15"
        assert data["failover_enabled"] is True
        assert data["failover_max_retries"] == 2

    @pytest.mark.parametrize("field", TRI_STATE_ROUTE_FIELDS)
    async def test_update_tri_state_passes_none_for_null_unset_for_omitted(
        self, client: AsyncClient, monkeypatch, field
    ):
        """三态透传：显式 null → service 收到 None（清空 / 归零）；键缺省 → UNSET（不改）。

        写入侧行为由 service 测试覆盖，此处只锁「清空信号不得在路由层被吞掉」。
        """
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen: dict = {}

        async def _fake_update(db, *, route_id, **kw):
            seen.update(kw)
            return _stub_route(id=route_id)

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "update_route", _fake_update)

        resp = await client.post("/api/v1/admin/publishes/1/update", json={field: None})
        assert resp.status_code == 200
        assert seen[field] is None, "显式 null 必须原样传 None（清空/归零），不得被吞成哨兵"
        for other in TRI_STATE_ROUTE_FIELDS:
            if other != field:
                assert seen[other] is publish_service.UNSET, f"{other} 未传应传哨兵（不改）"

        seen.clear()
        resp = await client.post("/api/v1/admin/publishes/1/update", json={})
        assert resp.status_code == 200
        for name in TRI_STATE_ROUTE_FIELDS:
            assert seen[name] is publish_service.UNSET, f"{name} 缺省应传哨兵（不改），否则保存会误清空未提交字段"

    @pytest.mark.parametrize("field", TRI_STATE_ROUTE_FIELDS)
    async def test_update_rejects_over_range_numbers(self, client: AsyncClient, field):
        """超长数字须拦在请求模型，422 带出错字段 loc（根因与上界来源见 schemas/request.py）。

        422 本身即证明未触达 service：FastAPI 在请求体校验阶段就拒绝，端点函数根本不执行。
        """
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        resp = await client.post("/api/v1/admin/publishes/1/update", json={field: 10**28})

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

    @pytest.mark.parametrize("field,value", _COLUMN_MAX_CASES)
    async def test_update_accepts_column_max_values(self, client: AsyncClient, monkeypatch, field, value):
        """上界本身是合法值（= 对应 DB 列的物理上界）：卡在列的容纳范围，不收得过紧。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen: dict = {}

        async def _fake_update(db, *, route_id, **kw):
            seen.update(kw)
            return _stub_route(id=route_id)

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "update_route", _fake_update)

        resp = await client.post("/api/v1/admin/publishes/1/update", json={field: value})

        assert resp.status_code == 200
        # 值须原样抵达 service（未被静默取整/截断）；价格字段经 Decimal 归一
        assert seen[field] == (Decimal(value) if isinstance(value, str) else value)

    @pytest.mark.parametrize("field", TRI_STATE_ROUTE_FIELDS)
    async def test_create_rejects_over_range_numbers(self, client: AsyncClient, field):
        """同 update 端点：创建路由时分发给同一批数值列的越界输入也须拦成 422（422 即未触达 service）。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        resp = await client.post("/api/v1/admin/publishes", json={"name": "m1", field: 10**28})

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

    @pytest.mark.parametrize("field,value", _COLUMN_MAX_CASES)
    async def test_create_accepts_column_max_values(self, client: AsyncClient, monkeypatch, field, value):
        """上界本身是合法值（= 对应 DB 列的物理上界）：卡在列的容纳范围，不收得过紧。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen: dict = {}

        async def _fake_create(db, **kw):
            seen.update(kw)
            return _stub_route(id=7, name="m1")

        async def _fake_bindings(db, *, route_id):
            return []

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "create_route", _fake_create)
        monkeypatch.setattr(route_mod, "list_bindings", _fake_bindings)

        resp = await client.post("/api/v1/admin/publishes", json={"name": "m1", field: value})

        assert resp.status_code == 200
        assert seen[field] == (Decimal(value) if isinstance(value, str) else value)

    async def test_create_route_conflict_maps_409(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        from llmpod.core.exceptions import Conflict

        async def _fake_create(db, **kw):
            raise Conflict("路由 'm1' 已存在")

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "create_route", _fake_create)

        resp = await client.post("/api/v1/admin/publishes", json={"name": "m1"})

        assert resp.status_code == 409

    async def test_create_route_passes_bindings_and_returns_them(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen = {}

        async def _fake_create(db, **kw):
            seen["bindings"] = kw.get("bindings")
            return _stub_route(id=7, name="m1")

        async def _fake_bindings(db, *, route_id):
            return [
                _stub_binding(id=1, route_id=route_id, endpoint_id=11, weight=3, priority=2),
                _stub_binding(id=2, route_id=route_id, endpoint_id=22),
            ]

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "create_route", _fake_create)
        monkeypatch.setattr(route_mod, "list_bindings", _fake_bindings)

        resp = await client.post(
            "/api/v1/admin/publishes",
            json={
                "name": "m1",
                "bindings": [
                    {"endpoint_id": 11, "weight": 3, "priority": 2},
                    {"endpoint_id": 22},
                ],
            },
        )

        assert resp.status_code == 200
        assert len(seen["bindings"]) == 2
        assert seen["bindings"][0].endpoint_id == 11 and seen["bindings"][0].weight == 3
        data = resp.json()["data"]
        assert data["route"]["id"] == 7
        assert [b["endpoint_id"] for b in data["bindings"]] == [11, 22]

    async def test_unpublish_maps_404(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        async def _fake_unpub(db, *, route_id):
            raise NotFound("no")

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "unpublish_route", _fake_unpub)

        resp = await client.post("/api/v1/admin/publishes/99/unpublish")
        assert resp.status_code == 404

    async def test_update_binding_maps_404(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        async def _fake_update(db, *, route_id, endpoint_id, weight=None, priority=None, enabled=None):
            raise NotFound("no")

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "update_binding", _fake_update)

        resp = await client.post("/api/v1/admin/publishes/1/bindings/99/update", json={"weight": 3})
        assert resp.status_code == 404

    async def test_disable_binding_round_trip(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        seen = {}

        async def _fake_update(db, *, route_id, endpoint_id, weight=None, priority=None, enabled=None):
            seen["enabled"] = enabled
            return _stub_binding(id=1, route_id=route_id, endpoint_id=endpoint_id, enabled=enabled)

        import llmpod.apps.webserver.routes.admin.publishes as route_mod

        monkeypatch.setattr(route_mod, "update_binding", _fake_update)

        resp = await client.post("/api/v1/admin/publishes/1/bindings/10/disable")

        assert resp.status_code == 200
        assert seen == {"enabled": False}
        assert resp.json()["data"]["enabled"] is False


class TestRequestLogsRoutes:
    async def test_routes_require_auth(self, client: AsyncClient):
        for path in ["/api/v1/admin/request-logs", "/api/v1/admin/request-logs/req-x"]:
            resp = await client.get(path)
            assert resp.status_code == 401, f"{path} 应受登录保护"

    async def test_list_returns_metering_rows(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        captured = {}

        async def _fake_list(
            db,
            *,
            page,
            page_size,
            tenant_id=None,
            api_key_id=None,
            model=None,
            status=None,
            conv_prefix_hash=None,
            since=None,
            until=None,
            exclude_tenant_scope=False,
        ):
            from llmpod.models import RequestLog

            captured["tenant_id"] = tenant_id
            captured["exclude_tenant_scope"] = exclude_tenant_scope
            row = RequestLog(
                id=1,
                request_id="req-1",
                tenant_id=None,
                api_key_id=1,
                model_alias="m1",
                served_model="internal",
                operation="chat.completions",
                protocol="openai",
                engine="vllm",
                prompt_tokens=3,
                completion_tokens=2,
                total_tokens=5,
                usage_estimated=False,
                cached_tokens=2,
                cost=None,
                latency_ms=100,
                status="success",
                http_status=200,
                request_ref=None,
                response_ref=None,
                conv_prefix_hash="abc",
                truncated=False,
                snapshot_disabled=True,
                error_message=None,
                retry_count=0,
                created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
            return [row], 1

        import llmpod.apps.webserver.routes.admin.request_logs as route_mod

        monkeypatch.setattr(route_mod, "list_request_logs", _fake_list)

        resp = await client.get("/api/v1/admin/request-logs")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert captured == {"tenant_id": None, "exclude_tenant_scope": True}  # 不传租户 → 平台视角（只看无归属计量行）
        assert data["total"] == 1
        assert data["items"][0]["model_alias"] == "m1"
        assert data["items"][0]["total_tokens"] == 5
        assert data["items"][0]["cached_tokens"] == 2  # 缓存命中列可见

    async def test_list_filters_by_tenant_id(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        seen: list[tuple[int | None, bool | None]] = []

        async def _fake_list(db, *, page, page_size, tenant_id=None, **kwargs):
            seen.append((tenant_id, kwargs.get("exclude_tenant_scope")))
            return [], 0

        import llmpod.apps.webserver.routes.admin.request_logs as route_mod

        monkeypatch.setattr(route_mod, "list_request_logs", _fake_list)

        # 带 tenant_id → 透传给 service，且关掉平台域限定
        resp = await client.get("/api/v1/admin/request-logs", params={"tenant_id": 42})
        assert resp.status_code == 200
        assert seen == [(42, False)]

        # 不带 tenant_id → 平台视角：平台域收敛开关置位（口径同审计日志：NULL ∪ 0）
        resp = await client.get("/api/v1/admin/request-logs")
        assert resp.status_code == 200
        assert seen == [(42, False), (None, True)]

        # 传 0 同样是平台视角（平台域哨兵，与审计页一致），不是「精确等值 0」
        resp = await client.get("/api/v1/admin/request-logs", params={"tenant_id": 0})
        assert resp.status_code == 200
        assert seen == [(42, False), (None, True), (0, True)]

    async def test_list_filters_by_time_range(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        seen: list[dict] = []

        async def _fake_list(db, *, page, page_size, **kwargs):
            seen.append({k: kwargs[k] for k in ("since", "until")})
            return [], 0

        import llmpod.apps.webserver.routes.admin.request_logs as route_mod

        monkeypatch.setattr(route_mod, "list_request_logs", _fake_list)

        # 毫秒时间戳（epoch 语义）→ route 转 UTC aware datetime 透传给 service
        resp = await client.get(
            "/api/v1/admin/request-logs",
            params={"since_ms": 1704067200000, "until_ms": 1735689599999},
        )
        assert resp.status_code == 200
        assert len(seen) == 1
        assert seen[0]["since"].tzinfo is not None
        assert seen[0]["until"].tzinfo is not None
        assert seen[0]["since"] < seen[0]["until"]

    async def test_detail_includes_bodies(self, client: AsyncClient, monkeypatch, tmp_path):
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        from llmpod.models import RequestLog

        row = RequestLog(
            id=1,
            request_id="req-1",
            tenant_id=None,
            api_key_id=1,
            model_alias="m1",
            served_model="internal",
            operation="chat.completions",
            protocol="openai",
            engine="vllm",
            prompt_tokens=3,
            completion_tokens=2,
            total_tokens=5,
            usage_estimated=False,
            cost=None,
            latency_ms=100,
            status="success",
            http_status=200,
            request_ref=None,
            response_ref=None,
            conv_prefix_hash=None,
            truncated=False,
            snapshot_disabled=False,
            error_message=None,
            retry_count=0,
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

        async def _fake_get(db, *, request_id):
            return row

        import llmpod.apps.webserver.routes.admin.request_logs as route_mod

        monkeypatch.setattr(route_mod, "get_request_log", _fake_get)

        from llmpod.apps.proxyserver.bodystore import LocalFileBodyStore

        store = LocalFileBodyStore(root=tmp_path)
        req_hash = store.put('{"messages": []}')
        row.request_ref = req_hash
        monkeypatch.setattr(route_mod, "get_body_store", lambda: store)

        resp = await client.get("/api/v1/admin/request-logs/req-1")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["request_body"] == {"messages": []}  # 详情读块还原 body
