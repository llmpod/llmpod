"""全局设置集成测试：公开/管理端路由契约、权限门、维护期登录拦截、种子注入。

遵循 stub 范式（conftest client 覆盖 get_db；登录态用例内覆盖 get_current_user；
service 调用 monkeypatch 路由模块命名空间符号）。HTTP 层只验路由注册、依赖链、
Result 契约与领域异常→HTTP 映射。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from llmpod.app import app
from _fakes import admin_session

import llmpod
import llmpod.apps.webserver.frontend as frontend_mod
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import LoginResult, SessionData
from llmpod.core.exceptions import ErrorCode
from llmpod.services.settings_service import SettingsPayload, MaintenanceState


def _fake_db_ctx(session=None):
    @asynccontextmanager
    async def _ctx():
        yield session

    return _ctx


async def _guide_closed(db):
    """first_guide_open 桩：不弹（终态/读失败口径）。"""
    return False


async def _guide_open(db):
    """first_guide_open 桩：待弹出（系统首次登录后、管理端首落地前）。"""
    return True


async def _deny(*_a, **_k):
    """内层权限校验无权限：user_has_permission 恒 False → 403。"""
    return False


class TestPublicSettings:
    async def test_get_settings_requires_login(self, client: AsyncClient):
        """匿名读：401——首屏已由 index.html 变量覆盖，接口只服务登录后刷新。"""
        response = await client.get("/api/v1/settings")
        assert response.status_code == 401
        assert response.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value

    async def test_get_settings_with_any_login(self, client: AsyncClient, monkeypatch):
        """任意登录态（不限访问域）：200 + Result 契约 + 未配置字段显式 null。"""

        async def fake_get(db):
            return SettingsPayload(siteName="站名", themeColor="#1a2b3c")

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.settings.get_settings", fake_get)
        app.dependency_overrides[get_current_user] = lambda: _session("tenant")
        response = await client.get("/api/v1/settings")
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 200
        data = body["data"]
        assert data["siteName"] == "站名"
        assert data["themeColor"] == "#1a2b3c"
        # 未配置字段为显式 null（区别于缺省键），表单回显判空统一 ?? ''
        assert data["logo"] is None
        assert data["maintenanceMode"] is False
        assert data["maintenanceMessage"] is None
        assert data["telemetryEnabled"] is True  # 遥测开关键恒存在，缺省=开


class TestAdminSettings:
    async def test_admin_get_with_meta(self, client: AsyncClient, monkeypatch):
        """管理端读：service get_settings_meta 一次点查返回 payload + 更新元数据。"""

        async def fake_meta(db):
            return (
                SettingsPayload(siteName="站名"),
                3,
                datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.settings.get_settings_meta", fake_meta)
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.get("/api/v1/admin/settings")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["siteName"] == "站名"
        assert data["updated_by"] == 3
        assert data["updated_at"] is not None

    async def test_admin_get_requires_login(self, client: AsyncClient):
        """无登录访问管理端读：外层门禁 require_platform_role → 401。"""
        response = await client.get("/api/v1/admin/settings")
        assert response.status_code == 401
        assert response.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value

    async def test_admin_get_denied_without_permission(self, client: AsyncClient, monkeypatch):
        """非超管且无 settings 读权限 → 403。"""

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _deny)
        app.dependency_overrides[get_current_user] = lambda: admin_session(is_superuser=False)

        response = await client.get("/api/v1/admin/settings")
        assert response.status_code == 403

    async def test_update_subset_patch(self, client: AsyncClient, monkeypatch):
        """三态部分更新：只传 maintenanceMode → update_settings 收到的 changes 只含该键。"""
        captured = {}

        async def fake_update(db, changes, updated_by):
            captured["changes"] = changes
            captured["updated_by"] = updated_by
            return SettingsPayload(maintenanceMode=changes["maintenanceMode"], siteName="库中保留")

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.settings.update_settings", fake_update)
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.post("/api/v1/admin/settings/update", json={"maintenanceMode": True})
        assert response.status_code == 200
        assert captured["changes"] == {"maintenanceMode": True}
        assert captured["updated_by"] == 1
        data = response.json()["data"]
        assert data["maintenanceMode"] is True
        assert data["siteName"] == "库中保留"

    async def test_update_allows_empty_string_clear(self, client: AsyncClient, monkeypatch):
        """空串=清空（三态之一）：请求体空串原样传给 service（exclude_unset 不剔除）。"""
        captured = {}

        async def fake_update(db, changes, updated_by):
            captured["changes"] = changes
            return SettingsPayload()

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.settings.update_settings", fake_update)
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.post("/api/v1/admin/settings/update", json={"footerText": "", "siteName": None})
        assert response.status_code == 200
        assert captured["changes"] == {"footerText": "", "siteName": None}

    async def test_update_denied_without_permission(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _deny)
        app.dependency_overrides[get_current_user] = lambda: admin_session(is_superuser=False)

        response = await client.post("/api/v1/admin/settings/update", json={"maintenanceMode": True})
        assert response.status_code == 403

    async def test_update_telemetry_toggle(self, client: AsyncClient, monkeypatch):
        """关遥测开关：False 直通 service（exclude_unset 不剔除假值），响应回显关。"""
        captured = {}

        async def fake_update(db, changes, updated_by):
            captured["changes"] = changes
            return SettingsPayload(telemetryEnabled=changes["telemetryEnabled"], siteName="库中保留")

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.settings.update_settings", fake_update)
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.post("/api/v1/admin/settings/update", json={"telemetryEnabled": False})
        assert response.status_code == 200
        assert captured["changes"] == {"telemetryEnabled": False}
        data = response.json()["data"]
        assert data["telemetryEnabled"] is False
        assert data["siteName"] == "库中保留"

    async def test_update_telemetry_null_rejected_422(self, client: AsyncClient):
        """显式 null 被 pydantic 请求模型先拦（布尔字段无 null 清空态）：
        422 + VALIDATION_ERROR，data 为错误明细数组——前端可见契约。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.post("/api/v1/admin/settings/update", json={"telemetryEnabled": None})
        assert response.status_code == 422
        body = response.json()
        assert body["error_code"] == ErrorCode.VALIDATION_ERROR.value
        assert body["data"][0]["loc"] == ["body", "telemetryEnabled"]

    async def test_update_bool_string_coerced(self, client: AsyncClient, monkeypatch):
        """pydantic lax 矫正现状："false"/0/1 自动转 bool 后照常生效（锁定该口径）。"""
        captured = {}

        async def fake_update(db, changes, updated_by):
            captured["changes"] = changes
            return SettingsPayload(**changes)

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.settings.update_settings", fake_update)
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.post("/api/v1/admin/settings/update", json={"telemetryEnabled": "false"})
        assert response.status_code == 200
        assert captured["changes"] == {"telemetryEnabled": False}


def _session(access_domain: str, is_superuser: bool = False) -> SessionData:
    return SessionData(
        user_id=7,
        tenant_id=5 if access_domain == "tenant" else 0,
        username="alice",
        role_codes=["tenant_admin" if access_domain == "tenant" else "operator"],
        access_domain=access_domain,
        is_superuser=is_superuser,
    )


class TestLoginMaintenance:
    """维护期登录拦截：验密后、签发前；平台域/超管放行，其余域 503 + 审计。"""

    async def _login(self, client: AsyncClient, monkeypatch, session: SessionData):
        async def fake_authenticate(db, username, password, settings):
            return LoginResult(token="stub-token", session=session)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.authenticate", fake_authenticate)

        # 首次引导弹窗触发是登录链路的真实落库逻辑（本会话为断开 DB 的桩），
        # 同样 patch 路由模块符号——HTTP 层只验契约，不跑 service 真实现
        async def skip_open_guide(db, *, user_id):
            return None

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.open_first_guide", skip_open_guide)
        return await client.post("/api/v1/auth/login", json={"username": "alice", "password": "pw"})

    async def _enable_maintenance(self, monkeypatch, message="升级维护中"):
        async def fake_maintenance(db):
            return MaintenanceState(enabled=True, message=message)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.get_maintenance", fake_maintenance)

    async def test_tenant_domain_rejected_503(self, client: AsyncClient, monkeypatch):
        """维护期租户域登录：503 + error_code=MAINTENANCE + failed 审计 + 撤销刚签发会话。"""
        await self._enable_maintenance(monkeypatch)
        audits = []
        logged_out = []

        async def fake_audit(**kwargs):
            audits.append(kwargs)

        async def fake_logout(token, settings):
            logged_out.append(token)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.write_audit", fake_audit)
        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.logout", fake_logout)

        response = await self._login(client, monkeypatch, _session("tenant"))

        assert response.status_code == 503
        body = response.json()
        assert body["error_code"] == "MAINTENANCE"
        assert body["message"] == "升级维护中"
        assert logged_out == ["stub-token"]  # fix #1：被拒登录不遗留 Redis 孤儿会话
        assert len(audits) == 1
        assert audits[0]["result"] == "failed"
        assert audits[0]["detail"] == {"error": "MAINTENANCE"}
        assert audits[0]["actor_id"] == 7

    async def test_platform_domain_allowed(self, client: AsyncClient, monkeypatch):
        """维护期平台域登录放行（管理员必须能进来关开关）。"""
        await self._enable_maintenance(monkeypatch)

        async def fail_failed_audit(**kwargs):
            assert kwargs.get("result") != "failed", "维护放行路径不应写 failed 审计"

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.write_audit", fail_failed_audit)
        response = await self._login(client, monkeypatch, _session("platform", is_superuser=False))
        assert response.status_code == 200
        assert response.json()["data"]["token"] == "stub-token"

    async def test_superuser_allowed(self, client: AsyncClient, monkeypatch):
        await self._enable_maintenance(monkeypatch)
        response = await self._login(client, monkeypatch, _session("tenant", is_superuser=True))
        assert response.status_code == 200

    async def test_not_maintenance_allows_tenant(self, client: AsyncClient, monkeypatch):
        """非维护期：租户域登录照常。"""
        response = await self._login(client, monkeypatch, _session("tenant"))
        assert response.status_code == 200


def _db_ctx(exc: Exception | None = None):
    """get_db_context 替身：yield 一个哑对象；exc 非空则在进入时抛出（模拟 DB 故障）。"""

    @asynccontextmanager
    async def _ctx():
        if exc is not None:
            raise exc
        yield object()

    return _ctx


class TestMaintenanceGateMiddleware:
    """维护闸中间件：maintenanceMode 开启时，把非平台侧的已登录 API 会话踢回登录页（401）。

    登录拦截早已存在，这里覆盖此前缺口：存量会话的 API 请求无任何拦截。
    中间件判定链用延迟导入，monkeypatch 源模块属性即可生效；
    豁免路径（login/settings 等）与匿名请求不触闸。
    """

    async def _gate_on(self, monkeypatch, session: SessionData | None, message: str = "升级维护中"):
        """维护态=开 + Redis 会话查证返回给定 SessionData（None=会话不存在）。"""

        async def fake_maintenance(db):
            return MaintenanceState(enabled=True, message=message)

        class _FakeRedis:
            async def get(self, key):
                return session.model_dump_json() if session is not None else None

        monkeypatch.setattr("llmpod.services.settings_service.get_maintenance", fake_maintenance)
        monkeypatch.setattr("llmpod.db.pg.get_db_context", _db_ctx())
        monkeypatch.setattr("llmpod.db.redis.get_redis", lambda: _FakeRedis())

    async def test_tenant_session_kicked_401(self, client: AsyncClient, monkeypatch):
        """维护期租户（非平台侧）登录会话调 API：401 + MAINTENANCE 码（前端据此跳登录页）。"""
        await self._gate_on(monkeypatch, _session("tenant"))
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 401
        body = resp.json()
        assert body["error_code"] == "MAINTENANCE"
        assert body["message"] == "升级维护中"

    async def test_superuser_passes_gate(self, client: AsyncClient, monkeypatch):
        """超管会话放行（能进来关开关）；下游 /me 走依赖覆盖返回 200。"""
        await self._gate_on(monkeypatch, _session("platform", is_superuser=True))
        app.dependency_overrides[get_current_user] = lambda: _session("platform", is_superuser=True)
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 200

    async def test_platform_operator_passes_gate(self, client: AsyncClient, monkeypatch):
        """平台域非超管（operator）同样放行：口径与登录拦截一致。

        否则「登录放行（auth.py 豁免平台域）→ 首次调 API 被闸踢回登录页 → 再登录
        放行」会无限跳转。镜像登录侧 test_platform_domain_allowed。
        """
        await self._gate_on(monkeypatch, _session("platform"))
        app.dependency_overrides[get_current_user] = lambda: _session("platform")
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 200

    async def test_expired_session_blocked_like_anonymous_401(self, client: AsyncClient, monkeypatch):
        """会话已过期（Redis 无记录）：闸视同非平台侧 → 401 + MAINTENANCE 优先于鉴权层。

        维护期整体降噪：不暴露「token 是否有效」的差别——已过期会话与有效租户会话
        同为 401，故用 error_code 区分「闸先行」与鉴权层的 TOKEN_EXPIRED/
        UNAUTHENTICATED。
        """
        await self._gate_on(monkeypatch, None)
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "MAINTENANCE"

    async def test_exempt_path_reaches_endpoint_with_token(self, client: AsyncClient, monkeypatch):
        """豁免必须真的被认：维护开启 + 租户会话带 token，豁免路径仍到达端点。

        若闸不认豁免，这里会拿到 401 + MAINTENANCE（=被闸拦）而非端点响应。
        """
        await self._gate_on(monkeypatch, _session("tenant"))
        app.dependency_overrides[get_current_user] = lambda: _session("tenant")

        async def fake_get_settings(db):
            return SettingsPayload()

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.settings.get_settings", fake_get_settings)
        resp = await client.get("/api/v1/settings", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 200, "settings 豁免：带 token 也应到端点，不被闸拦"

    async def test_login_exempt_and_anonymous_pass(self, client: AsyncClient, monkeypatch):
        """login 豁免维护闸；匿名（无 Bearer）交由鉴权层照常 401，不经闸判。"""
        await self._gate_on(monkeypatch, _session("tenant"))

        async def fake_authenticate(db, username, password, settings):
            from llmpod.core.exceptions import AuthError

            raise AuthError("bad credentials")

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.authenticate", fake_authenticate)
        r_login = await client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})
        assert r_login.status_code == 401, "login 豁免维护闸，走自身凭据错误 401"
        r_settings = await client.get("/api/v1/settings")
        assert r_settings.status_code == 401, "settings 匿名照常 401（豁免不被闸拦）"
        r_anon = await client.get("/api/v1/auth/me")
        assert r_anon.status_code == 401, "匿名照常 401，非维护闸 401"

    async def test_not_maintenance_allows_all(self, client: AsyncClient, monkeypatch):
        """非维护期：普通会话照常过闸。"""

        async def fake_maintenance(db):
            return MaintenanceState(enabled=False)

        monkeypatch.setattr("llmpod.services.settings_service.get_maintenance", fake_maintenance)
        monkeypatch.setattr("llmpod.db.pg.get_db_context", _db_ctx())
        app.dependency_overrides[get_current_user] = lambda: _session("tenant")
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 200

    async def test_gate_state_unreadable_fails_open(self, client: AsyncClient, monkeypatch):
        """维护态本身读不到（DB 抖动）→ 放行：拦会把未开维护的平台整体打死。"""
        monkeypatch.setattr("llmpod.db.pg.get_db_context", _db_ctx(RuntimeError("db down")))
        app.dependency_overrides[get_current_user] = lambda: _session("tenant")
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 200

    async def test_gate_exempt_check_failure_fails_closed(self, client: AsyncClient, monkeypatch):
        """维护已开启、豁免判定链故障（Redis 抖动）→ fail-closed：按维护拦截，不放普通会话过去。"""

        async def fake_maintenance(db):
            return MaintenanceState(enabled=True, message="升级维护中")

        monkeypatch.setattr("llmpod.services.settings_service.get_maintenance", fake_maintenance)
        monkeypatch.setattr("llmpod.db.pg.get_db_context", _db_ctx())

        def _redis_boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr("llmpod.db.redis.get_redis", _redis_boom)
        app.dependency_overrides[get_current_user] = lambda: _session("tenant")
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "MAINTENANCE"

    async def test_maintenance_401_passes_through_cors(self, client: AsyncClient, monkeypatch):
        """闸自身构造的 401 也要经 CORS 外层处理：跨域部署下浏览器才读得到 401 并跳登录页。

        CORS 默认已收敛为空（同源），且白名单在 app 构造时被中间件实例固化——
        直接改已建栈里 CORSMiddleware 实例的 allow_origins 再验。
        """
        from llmpod.app import app

        if app.middleware_stack is None:
            app.middleware_stack = app.build_middleware_stack()
        mw = app.middleware_stack
        while mw is not None:
            if type(mw).__name__ == "CORSMiddleware":
                mw.allow_origins = ["http://localhost:5173"]
                break
            mw = getattr(mw, "app", None)
        await self._gate_on(monkeypatch, _session("tenant"))
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer t", "Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 401
        assert resp.headers.get("access-control-allow-origin"), "维护 401 必须带 CORS 头"

    async def test_downstream_exception_not_swallowed_by_gate(self, client: AsyncClient, monkeypatch):
        """业务端点抛异常不被闸吞：不记「闸判定失败」误导日志、下游只跑一遍。

        用自定义 transport 直连 app（raise_server_exceptions=False），观测 500 响应
        而非异常上抛；服务端以计数器断言端点只执行一次（旧写法把 call_next 包进
        try 会吞异常后二次调用下游链）。
        """
        await self._gate_on(monkeypatch, _session("tenant", is_superuser=True))
        calls = {"n": 0}

        async def probe() -> dict:
            calls["n"] += 1
            raise RuntimeError("endpoint boom")

        from fastapi.routing import APIRoute
        from httpx import ASGITransport, AsyncClient as _AC

        from llmpod.app import app as main_app

        # 探针不能追加到路由表尾：本机存在前端构建产物时 SPA 兜底
        # ``/{full_path:path}`` 已在建应用期注册，晚注册的 API 路由会被它遮蔽
        # （api/ 前缀在兜底里统一 404）——插到兜底之前保证探针可达。
        probe_route = APIRoute("/api/v1/_gate_probe", endpoint=probe, methods=["GET"])
        routes = main_app.router.routes
        fallback_idx = next(
            (i for i, r in enumerate(routes) if getattr(r, "path", "") == "/{full_path:path}"),
            len(routes),
        )
        routes.insert(fallback_idx, probe_route)
        try:
            async with _AC(
                transport=ASGITransport(app=main_app, raise_app_exceptions=False),
                base_url="http://test",
            ) as probe_client:
                resp = await probe_client.get("/api/v1/_gate_probe", headers={"Authorization": "Bearer t"})
                assert resp.status_code == 500
                assert calls["n"] == 1, "端点只能执行一次（闸不重跑下游链）"
        finally:
            routes.remove(probe_route)


class TestSeedInjection:
    """index.html 全局设置下发：脚本位置、变量形态、空值 null、DB 失败不注入。"""

    @pytest.fixture(autouse=True)
    def _guide_closed_default(self, monkeypatch):
        """本组用例默认不弹；待弹出/消耗用例在体内另行覆盖。"""
        monkeypatch.setattr(frontend_mod, "first_guide_open", _guide_closed)

    async def test_var_shape_parity_with_api(self, client: AsyncClient, monkeypatch):
        """契约锁定：注入变量 = GET /settings 的 data **加只读附加键 serverVersion /
        first_guide**（两键仅注入下发、不进设置 API 契约）。配置字段集、键序、值与
        API 完全一致（前端消费共用一套类型，改字段必须同改）。"""
        import json
        import re

        payload = SettingsPayload(
            siteName="站名",
            themeDescription="描述",
            themeColor="#1a2b3c",
            mode="dark",
            loginBackground="https://x/bg.png",
            loginPrimary="#abc",
            maintenanceMode=True,
            maintenanceMessage="升级中",
        )

        async def fake_settings(db):
            return payload

        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())
        monkeypatch.setattr(frontend_mod, "get_settings_or_none", fake_settings)
        monkeypatch.setattr("llmpod.apps.webserver.routes.public.settings.get_settings", fake_settings)
        app.dependency_overrides[get_current_user] = lambda: _session("platform", is_superuser=True)

        html = (await client.get("/")).text
        injected = json.loads(re.search(r"window\.__LLMPOD_SETTINGS__=(.*?);</script>", html, re.S).group(1))
        api_data = (await client.get("/api/v1/settings")).json()["data"]
        # 版本与引导只出现在注入变量，API data 不含
        assert injected.pop("serverVersion") == llmpod.__version__
        assert injected.pop("first_guide") is False
        assert injected == api_data
        assert list(injected) == list(SettingsPayload.model_fields)  # 配置字段键序也锁死

    async def test_var_injected_before_boot_script(self, client: AsyncClient, monkeypatch):
        """有配置：下发脚本位于 <head> 后、既有内联启动脚本之前，payload 完整下发。"""
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())

        async def fake_settings(db):
            return SettingsPayload(
                mode="dark",
                themeColor="#1a2b3c",
                siteName="站名",
                logo="🚀",
                loginPrimary="#abc",
            )

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", fake_settings)
        response = await client.get("/")
        assert response.status_code == 200
        html = response.text
        head_end = html.index("<head>")
        var_pos = html.index("window.__LLMPOD_SETTINGS__")
        boot_pos = html.index("首屏主题启动脚本") if "首屏主题启动脚本" in html else html.index("<script>", var_pos + 1)
        assert head_end < var_pos
        assert var_pos < boot_pos
        # 契约 = 公开读接口 data 原样：themeColor 不做任何改名映射
        assert '"themeColor":"#1a2b3c"' in html
        assert '"mode":"dark"' in html
        assert '"siteName":"站名"' in html
        assert '"logo":"🚀"' in html
        assert '"loginPrimary":"#abc"' in html

    async def test_full_payload_with_null_fields(self, client: AsyncClient, monkeypatch):
        """部分配置：未配置字段以显式 null 下发（与 GET data 同形，含维护字段）。"""
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())

        async def only_site_name(db):
            return SettingsPayload(siteName="只有站名")

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", only_site_name)
        response = await client.get("/")
        html = response.text
        assert '"siteName":"只有站名"' in html
        assert '"themeColor":null' in html
        assert '"maintenanceMode":false' in html

    async def test_maintenance_only_still_delivered(self, client: AsyncClient, monkeypatch):
        """仅开维护开关（其余全空）：仍下发对象——登录页横幅读变量里的维护字段。"""
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())

        async def only_maintenance(db):
            return SettingsPayload(maintenanceMode=True)

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", only_maintenance)
        response = await client.get("/")
        assert '"maintenanceMode":true' in response.text

    async def test_all_empty_injects_null(self, client: AsyncClient, monkeypatch):
        """未配置/全空 + 弹窗不弹：变量置 null（前端清缓存回落默认），而非缺字段对象。

        null 清缓存信号优先——全空时不携带 serverVersion（前端此时拿版本走
        /api/v1/version/latest 的 current_version）。
        """
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())

        async def nothing(db):
            return SettingsPayload()

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", nothing)
        response = await client.get("/")
        assert "window.__LLMPOD_SETTINGS__=null;" in response.text

    async def test_first_guide_open_overrides_null(self, client: AsyncClient, monkeypatch):
        """配置全空但首次引导弹窗待弹出：不注入 null 而是全字段 null + first_guide=true 的对象。

        null 会让前端清缓存回落默认（等价语义：boot 桥接对全空 seed 同样清缓存），
        但管理端守卫读的是 window 变量本身——置 null 引导就丢了。引导是系统状态
        而非可配置字段，不受「管理员清空设置」影响。
        """
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())
        monkeypatch.setattr(frontend_mod, "first_guide_open", _guide_open)

        async def nothing(db):
            return SettingsPayload()

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", nothing)
        response = await client.get("/")
        html = response.text
        assert "window.__LLMPOD_SETTINGS__=null;" not in html
        assert '"first_guide":true' in html
        assert '"siteName":null' in html
        assert '"maintenanceMode":false' in html
        # 待弹出注入也是「有配置」形态：版本一并携带
        assert '"serverVersion":' in html

    async def test_admin_landing_consumes_window(self, client: AsyncClient, monkeypatch):
        """待弹出 + 管理端入口：本页仍注入 true（读到消耗前的待弹出态），且随读
        永久消耗弹出机会——自动弹出整个系统只有一次，跳过/完成后换标签页不再弹。"""
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())
        consumed = []

        async def fake_open(db):
            return True

        async def fake_consume(db):
            consumed.append(True)

        monkeypatch.setattr(frontend_mod, "first_guide_open", fake_open)
        monkeypatch.setattr(frontend_mod, "consume_first_guide", fake_consume)

        async def nothing(db):
            return SettingsPayload()

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", nothing)
        response = await client.get("/admin/")
        assert '"first_guide":true' in response.text
        assert consumed == [True]

    async def test_non_admin_entries_do_not_consume(self, client: AsyncClient, monkeypatch):
        """根入口与租户入口只读不耗：守卫只在管理端消费 first_guide，在别处消耗
        等于白白把仅有的一次弹出机会弹没（待弹出态对这两端注入是无害的空操作）。"""
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())
        consumed = []

        async def fake_consume(db):
            consumed.append(True)

        monkeypatch.setattr(frontend_mod, "first_guide_open", _guide_open)
        monkeypatch.setattr(frontend_mod, "consume_first_guide", fake_consume)

        async def nothing(db):
            return SettingsPayload()

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", nothing)
        for path in ("/", "/tenant/some/deep/link"):
            response = await client.get(path)
            assert '"first_guide":true' in response.text
        assert consumed == []

    async def test_first_guide_false_plain_object(self, client: AsyncClient, monkeypatch):
        """有配置 + 弹窗不弹：first_guide=false 随对象下发（键恒存在，前端只认 true）。"""
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())

        async def fake_settings(db):
            return SettingsPayload(siteName="站名")

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", fake_settings)
        response = await client.get("/")
        assert '"siteName":"站名"' in response.text
        assert '"first_guide":false' in response.text

    async def test_guide_read_failure_keeps_seed(self, client: AsyncClient, monkeypatch):
        """设置读取成功但引导会话挂掉：种子照常注入（first_guide=false）。

        引导读走独立短会话——若与设置共用 session，引导读毒化的事务会让收尾
        commit 跟着报错，把**已读到的设置种子**一并丢掉（整页回落原始 HTML）。
        两段式 ctx：第一次进入（设置）正常 yield，第二次（引导）进 ctx 即炸。
        """
        calls = {"n": 0}

        @asynccontextmanager
        async def two_phase_ctx():
            calls["n"] += 1
            if calls["n"] == 1:
                yield object()
                return
            raise RuntimeError("db down on guide session")

        monkeypatch.setattr(frontend_mod, "get_db_context", two_phase_ctx)

        async def fake_settings(db):
            return SettingsPayload(siteName="站名")

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", fake_settings)
        response = await client.get("/")
        assert response.status_code == 200
        assert '"siteName":"站名"' in response.text
        assert '"first_guide":false' in response.text

    async def test_db_failure_no_injection(self, client: AsyncClient, monkeypatch):
        """DB 失败且无缓存：原样返回，不注入下发脚本（变量保持 undefined）。

        前端 boot 桥接已上线，其注释文本里也含 ``window.__LLMPOD_SETTINGS__``，
        故断言用后端注入脚本的完整前缀（含赋值号），不撞前端注释。
        """
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())

        async def read_failed(db):
            return None

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", read_failed)
        response = await client.get("/")
        assert response.status_code == 200
        assert "window.__LLMPOD_SETTINGS__=" not in response.text

    async def test_script_escape_angle_bracket(self, client: AsyncClient, monkeypatch):
        """字段值含 ``<`` 时转义为 \\u003c，防 ``</script>`` 逃逸。"""
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())

        async def evil(db):
            return SettingsPayload(logo="</script><script>alert(1)</script>")

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", evil)
        response = await client.get("/")
        assert "</script>alert" not in response.text
        assert "\\u003c" in response.text

    async def test_admin_spa_also_seeded(self, client: AsyncClient, monkeypatch):
        """子 SPA（/admin 深链刷新）同样走注入路径。"""
        monkeypatch.setattr(frontend_mod, "get_db_context", _fake_db_ctx())

        async def fake_settings(db):
            return SettingsPayload(siteName="站名")

        monkeypatch.setattr(frontend_mod, "get_settings_or_none", fake_settings)
        response = await client.get("/admin/some/deep/link")
        assert response.status_code == 200
        assert '"siteName":"站名"' in response.text

    async def test_assets_cache_header_untouched(self, client: AsyncClient, monkeypatch):
        """assets 静态资源不走注入，immutable 缓存头保持。"""
        import llmpod

        static_dir = llmpod.__file__.replace("__init__.py", "") + "static/"
        asset = None
        for spa in ("", "admin/", "tenant/"):
            import pathlib

            for p in pathlib.Path(static_dir + spa + "assets").glob("*.js"):
                asset = spa + "assets/" + p.name
                break
            if asset:
                break
        assert asset, "构建产物缺 assets，先 make frontend-build"

        response = await client.get("/" + asset)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
