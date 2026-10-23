"""租户侧网关资源路由契约测试（api-keys / request-logs / wallet / models）。

遵循项目 HTTP 层测试原则：只验路由注册、依赖链（外层门禁 401 / tenant 域 403），
统一 Result 契约、领域异常→HTTP 映射与租户作用域强制；业务逻辑由 service 测试覆盖。
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
    admin_session,
    tenant_session,
)

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.core.exceptions import NotFound
from llmpod.db import get_redis
from llmpod.models import ApiKey, RequestLog, Route, TenantModelBlock, Wallet
from llmpod.services.api_key_service import UNSET


def _stub_key(id=1, **ovr) -> ApiKey:
    base = dict(
        id=id,
        key_hash="a" * 64,
        tenant_id=5,
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


def _stub_log(id=1, **ovr) -> RequestLog:
    base = dict(
        id=id,
        request_id=f"req-{id}",
        tenant_id=5,
        api_key_id=1,
        model_alias="m1",
        served_model="internal-m1",
        operation="chat.completions",
        protocol="openai",
        engine="vllm",
        upstream_type="external",
        provider_name="acme-vendor",
        endpoint_id=9,
        route_reason="failover_weighted",
        prompt_tokens=3,
        completion_tokens=2,
        total_tokens=5,
        usage_estimated=False,
        cached_tokens=None,
        cost=Decimal("0.00001"),
        latency_ms=12,
        status="success",
        http_status=200,
        request_ref=None,
        response_ref=None,
        conv_prefix_hash=None,
        truncated=False,
        snapshot_disabled=True,
        error_message=None,
        retry_count=0,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return RequestLog(**base)


def _stub_route(id=1, **ovr) -> Route:
    base = dict(
        id=id,
        name="m1",
        status="published",
        origin="manual",
        model_type="llm",
        description=None,
        input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("2"),
        cached_input_price_per_mtok=None,
        failover_enabled=False,
        failover_max_retries=0,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return Route(**base)


def _stub_block(id=1, **ovr) -> TenantModelBlock:
    base = dict(
        id=id,
        tenant_id=5,
        route_id=1,
        status="active",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return TenantModelBlock(**base)


def _stub_wallet(id=1, **ovr) -> Wallet:
    base = dict(
        id=id,
        tenant_id=5,
        balance=Decimal("100"),
        frozen_balance=Decimal("5"),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    base.update(ovr)
    return Wallet(**base)


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


class TestTenantApiKeysRoutes:
    async def test_routes_require_auth(self, client: AsyncClient):
        for method, path in [
            ("get", "/api/v1/tenant/api-keys"),
            ("post", "/api/v1/tenant/api-keys"),
            ("post", "/api/v1/tenant/api-keys/1/update"),
            ("post", "/api/v1/tenant/api-keys/1/disable"),
            ("post", "/api/v1/tenant/api-keys/1/enable"),
            ("post", "/api/v1/tenant/api-keys/1/delete"),
            ("post", "/api/v1/tenant/api-keys/1/reset-budget"),
        ]:
            resp = await client.request(method, path, json={})
            assert resp.status_code == 401, f"{method} {path} 应受登录保护"

    async def test_list_search_stays_in_tenant_scope(self, client: AsyncClient, monkeypatch):
        """列表搜索：作用域硬绑会话租户，tenant_id 查询参数被忽略（不提供跨租户搜索）。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        seen = {}

        async def _fake_list(db, *, tenant_id, name=None, status=None):
            seen.update(tenant_id=tenant_id, name=name, status=status)
            return [_stub_key(id=3, name="prod-key", status="disabled")]

        import llmpod.apps.webserver.routes.tenant.api_keys as route_mod

        monkeypatch.setattr(route_mod, "list_api_keys", _fake_list)

        # 故意带 tenant_id=999：FastAPI 忽略未知查询参数，作用域仍是会话租户 5
        resp = await client.get(
            "/api/v1/tenant/api-keys",
            params={"tenant_id": 999, "name": "prod", "status": "disabled"},
        )
        assert resp.status_code == 200
        assert seen["tenant_id"] == 5, "租户不能通过参数切换搜索作用域"
        assert seen["name"] == "prod"
        assert seen["status"] == "disabled"
        item = resp.json()["data"][0]
        assert item["name"] == "prod-key" and item["status"] == "disabled"

    async def test_platform_session_rejected_by_gate(self, client: AsyncClient):
        """外层门禁：平台域会话（access_domain=platform）不能访问租户路由。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        resp = await client.get("/api/v1/tenant/api-keys")
        assert resp.status_code == 403

    async def test_create_scopes_to_current_tenant(self, client: AsyncClient, monkeypatch, stub_redis):
        """签发的 Key 强制归属当前租户（tenant_id 取自会话，非请求体）。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        seen = {}

        async def _fake_create(db, redis, **kw):
            seen.update(kw)
            return _stub_key(id=9), "sk-tenant-once"

        import llmpod.apps.webserver.routes.tenant.api_keys as route_mod

        monkeypatch.setattr(route_mod, "create_api_key", _fake_create)

        resp = await client.post(
            "/api/v1/tenant/api-keys",
            json={
                "name": "k",
                "allowed_models": ["m1"],
            },
        )
        assert resp.status_code == 200
        assert seen["tenant_id"] == 5  # 归属强制 = 会话租户
        body = resp.json()
        assert body["data"]["plaintext"] == "sk-tenant-once"
        assert body["data"]["api_key"]["tenant_id"] == 5

    async def test_delete_foreign_key_404(self, client: AsyncClient, monkeypatch, stub_redis):
        """跨租户 Key 视同不存在：作用域查询由路由的 _load_tenant_key 强制。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        from tests.integration._fakes import FakeSession

        foreign_db = FakeSession()
        # 预置一行他租户 Key：若路由漏了 tenant_id 过滤，按 id 能查到 → 不再 404，
        # 测试即红——空库下 404 只证明「查无此行」，证明不了作用域（假绿）
        foreign_db.preload(
            ApiKey(
                id=99,
                tenant_id=7,  # 他租户
                key_hash="h-foreign",
                name="foreign",
                allowed_models=[],
                rpm_limit=None,
                tpm_limit=None,
                total_token_quota=None,
                model_token_quotas={},
                status="active",
            )
        )
        import llmpod.db as db_mod

        orig = db_mod.get_db

        async def _db_gen():
            yield foreign_db

        app.dependency_overrides[orig] = _db_gen
        try:
            resp = await client.post("/api/v1/tenant/api-keys/99/delete")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides[orig] = lambda: None

    async def test_update_tri_state_passes_none_for_null_unset_for_omitted(
        self, client: AsyncClient, monkeypatch, stub_redis
    ):
        """租户侧同样三态透传（作用域加载打桩，只验清空信号不被路由层吞掉）。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        seen: dict = {}

        async def _fake_load(db, key_id, tenant_id):
            return _stub_key(id=1)

        async def _fake_update(db, redis, **kw):
            seen.update(kw)
            return _stub_key(id=1)

        import llmpod.apps.webserver.routes.tenant.api_keys as route_mod

        monkeypatch.setattr(route_mod, "_load_tenant_key", _fake_load)
        monkeypatch.setattr(route_mod, "update_api_key", _fake_update)

        resp = await client.post("/api/v1/tenant/api-keys/1/update", json={"expires_at": None})
        assert resp.status_code == 200
        assert seen["expires_at"] is None, "显式 null 必须原样传 None（清空），不得被吞成哨兵"
        for field in CLEARABLE_KEY_FIELDS:
            if field != "expires_at":
                assert seen[field] is UNSET, f"{field} 未传应传哨兵（不改）"

        seen.clear()
        resp = await client.post("/api/v1/tenant/api-keys/1/update", json={})
        assert resp.status_code == 200
        for field in CLEARABLE_KEY_FIELDS:
            assert seen[field] is UNSET, f"{field} 缺省应传哨兵（不改），否则保存会误清空未提交字段"

    @pytest.mark.parametrize("field", KEY_QUOTA_FIELDS)
    async def test_create_rejects_over_range_quota(self, client: AsyncClient, stub_redis, field):
        """超长数字须拦在请求模型（422 + 字段 loc），不得直达 asyncpg 参数绑定。

        422 本身即证明未触达 service：FastAPI 在请求体校验阶段就拒绝，端点函数根本不执行。
        """
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        resp = await client.post("/api/v1/tenant/api-keys", json={"name": "k1", field: NUMERIC_OVERSIZED})

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

    @pytest.mark.parametrize("field", KEY_QUOTA_FIELDS)
    async def test_update_rejects_over_range_quota(self, client: AsyncClient, stub_redis, field):
        """同 create：租户侧更新路径的同一批字段同样须拦成 422。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        resp = await client.post("/api/v1/tenant/api-keys/1/update", json={field: NUMERIC_OVERSIZED})

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

    @pytest.mark.parametrize("field,value", KEY_QUOTA_MAX_CASES)
    async def test_update_accepts_column_max_quota(self, client: AsyncClient, monkeypatch, stub_redis, field, value):
        """上界本身是合法值（= 对应 DB 列的物理上界）：卡在列的容纳范围，不收得过紧。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        seen: dict = {}

        async def _fake_load(db, key_id, tenant_id):
            return _stub_key(id=1)

        async def _fake_update(db, redis, **kw):
            seen.update(kw)
            return _stub_key(id=1)

        import llmpod.apps.webserver.routes.tenant.api_keys as route_mod

        monkeypatch.setattr(route_mod, "_load_tenant_key", _fake_load)
        monkeypatch.setattr(route_mod, "update_api_key", _fake_update)

        resp = await client.post("/api/v1/tenant/api-keys/1/update", json={field: value})

        assert resp.status_code == 200
        # 值须原样抵达 service（未被静默取整/截断）；budget_limit 经 Decimal 归一
        assert seen[field] == (Decimal(value) if isinstance(value, str) else value)


class TestTenantRequestLogsRoutes:
    async def test_routes_require_auth(self, client: AsyncClient):
        for path in ("/api/v1/tenant/request-logs", "/api/v1/tenant/request-logs/req-x"):
            resp = await client.get(path)
            assert resp.status_code == 401, f"{path} 应受登录保护"

    async def test_list_passes_tenant_scope(self, client: AsyncClient, monkeypatch):
        """列表查询强制带 tenant_id（service 层作用域参数由路由注入）。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        seen = {}

        async def _fake_list(db, **kw):
            seen.update(kw)
            return [_stub_log(id=1)], 1

        import llmpod.apps.webserver.routes.tenant.request_logs as route_mod

        monkeypatch.setattr(route_mod, "list_request_logs", _fake_list)

        resp = await client.get("/api/v1/tenant/request-logs")
        assert resp.status_code == 200
        assert seen["tenant_id"] == 5
        item = resp.json()["data"]["items"][0]
        assert item["model_alias"] == "m1"
        # 运营侧字段剥离：供应商/内部端点/引擎身份/选路策略不外泄（即便行内非空）
        for hidden in ("served_model", "engine", "upstream_type", "provider_name", "endpoint_id", "route_reason"):
            assert hidden not in item, f"租户视图不应含 {hidden}"

    async def test_detail_foreign_request_404(self, client: AsyncClient, monkeypatch):
        """详情跨租户视同不存在（get_request_log 带 tenant_id 作用域）。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        async def _fake_get(db, *, request_id, tenant_id=None):
            assert tenant_id == 5  # 作用域参数确实传了
            raise NotFound("no")

        import llmpod.apps.webserver.routes.tenant.request_logs as route_mod

        monkeypatch.setattr(route_mod, "get_request_log", _fake_get)

        resp = await client.get("/api/v1/tenant/request-logs/req-other")
        assert resp.status_code == 404


class TestTenantWalletRoutes:
    async def test_routes_require_auth(self, client: AsyncClient):
        for path in ("/api/v1/tenant/wallet", "/api/v1/tenant/wallet/transactions"):
            resp = await client.get(path)
            assert resp.status_code == 401, f"{path} 应受登录保护"

    async def test_balance_three_parts(self, client: AsyncClient, monkeypatch):
        """余额三段：总余额 / 冻结 / 可用（无钱包行按 0）。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        async def _fake_snapshot(db, tenant_id):
            assert tenant_id == 5
            w = _stub_wallet()
            return w.balance, w.frozen_balance, w.balance - w.frozen_balance

        import llmpod.apps.webserver.routes.tenant.wallet as route_mod

        monkeypatch.setattr(route_mod, "wallet_snapshot", _fake_snapshot)

        resp = await client.get("/api/v1/tenant/wallet")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["balance"] == "100"
        assert data["frozen_balance"] == "5"
        assert data["available"] == "95"

    async def test_transactions_scoped(self, client: AsyncClient, monkeypatch):
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        seen = {}

        async def _fake_txns(db, *, tenant_id, page, page_size):
            seen.update(tenant_id=tenant_id)
            return [], 0

        import llmpod.apps.webserver.routes.tenant.wallet as route_mod

        monkeypatch.setattr(route_mod, "list_transactions", _fake_txns)

        resp = await client.get("/api/v1/tenant/wallet/transactions")
        assert resp.status_code == 200
        assert seen["tenant_id"] == 5


class TestTenantModelsRoutes:
    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/tenant/models")
        assert resp.status_code == 401

    async def test_lists_published_minus_blocked_routes(self, client: AsyncClient, monkeypatch):
        """可用模型 = 已发布 − 本租户 active 封禁；含定价（模型市场展示）。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        class _FakeResult:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def __iter__(self):
                return iter(self._rows)

        async def _fake_execute(entity_stmt):
            # db.execute 的桩：select(实体) 按首列实体分流；select(TenantModelBlock.route_id)
            # 精简列查询（entity 仍解析为 TenantModelBlock）按列名分流返回 route_id 标量流
            desc = entity_stmt.column_descriptions[0]
            if desc["entity"] is TenantModelBlock and desc.get("name") == "route_id":
                return _FakeResult([1])  # 仅 active 封禁 m1；disabled 不计
            entity = desc["entity"]
            if entity is TenantModelBlock:
                return _FakeResult(
                    [
                        _stub_block(id=1, route_id=1),  # m1 被封禁
                        _stub_block(id=2, route_id=2, status="disabled"),  # disabled 不封禁
                    ]
                )
            if entity is Route:
                # published 过滤在 SQL where（桩不解析条件），按「已发布集」喂行：
                # m1/m2 已发布，m3 未发布不会出现在该查询结果里
                return _FakeResult(
                    [
                        _stub_route(id=1, name="m1"),  # published 但被 active 封禁
                        _stub_route(id=2, name="m2"),  # published 且未被封禁
                    ]
                )
            return _FakeResult([])

        class _FakeDB:
            async def execute(self, stmt):
                return await _fake_execute(stmt)

        # 路由内的 db 来自 Depends(get_db)；这里直接覆盖 get_db 返回桩 session
        from llmpod.db import get_db as _get_db

        async def _db_gen():
            yield _FakeDB()

        app.dependency_overrides[_get_db] = _db_gen

        resp = await client.get("/api/v1/tenant/models")
        assert resp.status_code == 200
        items = resp.json()["data"]
        # active 封禁（m1）与 unpublished（m3）都被过滤，disabled 封禁（m2）保留
        assert [i["name"] for i in items] == ["m2"]
        assert items[0]["input_price_per_mtok"] == "1"


class TestProxyEndpointRoutes:
    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/proxy-endpoint")
        assert resp.status_code == 401

    async def test_platform_session_can_access(self, client: AsyncClient):
        """跨域：平台端（access_domain=platform）同样能读。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        resp = await client.get("/api/v1/proxy-endpoint")
        assert resp.status_code == 200
        assert "base_url" in resp.json()["data"]

    async def test_fallback_derives_from_request_host(self, client: AsyncClient, monkeypatch):
        """未配置宣告地址 → 按请求 Host 推导（Host 主机部分 + proxy_port）。"""
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "proxy_advertise_url", "")
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        resp = await client.get("/api/v1/proxy-endpoint")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["base_url"] == f"http://{client.base_url.host}:{settings.proxy_port}"
        assert data["advertise_url_configured"] is False

    async def test_fallback_follows_lan_ip_visitor(self, client: AsyncClient, monkeypatch):
        """内网 IP 访问 Manager → 返回同一 LAN IP + 网关端口；公网域名访问同理。"""
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "proxy_advertise_url", "")
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        resp = await client.get("/api/v1/proxy-endpoint", headers={"Host": "192.168.1.10"})
        assert resp.status_code == 200
        assert resp.json()["data"]["base_url"] == f"http://192.168.1.10:{settings.proxy_port}"

    async def test_fallback_skips_unroutable_host(self, client: AsyncClient, monkeypatch):
        """Host 是监听通配地址（0.0.0.0）视为未提供 → 回退 settings 拼接。"""
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "proxy_advertise_url", "")
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        resp = await client.get("/api/v1/proxy-endpoint", headers={"Host": "0.0.0.0"})
        assert resp.status_code == 200
        assert resp.json()["data"]["base_url"] == f"http://{settings.proxy_host}:{settings.proxy_port}"

    async def test_advertise_url_preferred(self, client: AsyncClient, monkeypatch):
        """配置了宣告地址 → 优先返回它（去尾部斜杠）。"""
        from llmpod.config.settings import settings

        monkeypatch.setattr(settings, "proxy_advertise_url", "https://api.example.com/v1/")
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        resp = await client.get("/api/v1/proxy-endpoint")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["base_url"] == "https://api.example.com/v1"
        assert data["advertise_url_configured"] is True
