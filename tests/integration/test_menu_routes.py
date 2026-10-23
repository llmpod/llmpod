"""菜单可见性集成测试：/auth/menus 字典契约 + /auth/menus/update 自助写。

遵循 stub 范式（conftest client 覆盖 get_db；登录态用例内覆盖 get_current_user；
service 调用 monkeypatch 路由模块命名空间符号）。HTTP 层只验路由注册、依赖链、
Result 契约与领域异常→HTTP 映射。自助端点只作用于当前会话用户，不收 user_id。
"""

from __future__ import annotations

from httpx import AsyncClient

from llmpod.app import app
from llmpod.config.constants import PLATFORM_TENANT_ID
from _fakes import admin_session, tenant_session

from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.core.exceptions import ErrorCode, LLMPodValidationError


async def _deny(*_a, **_k):
    """内层权限校验无权限：user_has_permission 恒 False → 403。"""
    return False


async def _only_dashboard_perms(_db, _uid):
    """非超管权限并集桩：仅持 dashboard:read。"""
    return {"platform:dashboard:read"}


async def _dashboard_user_disabled(_db, _uid, _scope):
    """用户自助开关桩：dashboard 被本人下线。"""
    return frozenset({"/admin/dashboard"})


async def _playground_user_disabled(_db, _uid, _scope):
    """用户自助开关桩：playground 被本人下线。"""
    return frozenset({"/tenant/playground"})


async def _no_audit(_entry):
    """审计落库桩：写端点测试不打真实 PG。"""
    return None


class TestAuthMenus:
    """/auth/menus：字典形状（key=路由路径，value 仅 enabled）+ 按域分发。"""

    async def test_requires_login(self, client: AsyncClient):
        response = await client.get("/api/v1/auth/menus")
        assert response.status_code == 401
        assert response.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value

    async def test_superuser_platform_full_set_dict_shape(self, client: AsyncClient):
        """超管：platform 域全集下发；条目 value 只有 enabled 一个键。"""
        app.dependency_overrides[get_current_user] = lambda: admin_session()
        response = await client.get("/api/v1/auth/menus")
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 200
        data = body["data"]
        assert isinstance(data, dict) and len(data) >= 15
        assert all(isinstance(v, dict) for v in data.values())
        # 契约锁定：value 仅 enabled（title/排序前端自持）
        assert all(set(v) == {"enabled"} for v in data.values())
        assert all(isinstance(v["enabled"], bool) for v in data.values())
        # 平台账号拿 platform 菜单（不泄露租户侧结构）
        assert "/admin/dashboard" in data
        assert not any(k.startswith("/tenant/") for k in data)

    async def test_tenant_domain_gets_tenant_side(self, client: AsyncClient):
        """租户域：只下发 tenant 侧菜单。"""
        app.dependency_overrides[get_current_user] = lambda: tenant_session()
        response = await client.get("/api/v1/auth/menus")
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) >= 8
        assert all(k.startswith("/tenant/") for k in data)

    async def test_switch_off_marks_false_but_keeps_key(self, client: AsyncClient, monkeypatch):
        """本人开关：关掉的菜单仍在返回中标 enabled=False（呈现层，非数据层）。"""
        monkeypatch.setattr("llmpod.services.menu_service.get_user_disabled_menus", _dashboard_user_disabled)
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.get("/api/v1/auth/menus")
        assert response.status_code == 200
        data = response.json()["data"]
        assert "/admin/dashboard" in data
        assert data["/admin/dashboard"]["enabled"] is False

    async def test_normal_user_filtered_by_required(self, client: AsyncClient, monkeypatch):
        """非超管：持 dashboard:read → dashboard + 空 required 菜单可见，其余不进返回。"""
        monkeypatch.setattr("llmpod.services.menu_service.get_user_permissions", _only_dashboard_perms)
        app.dependency_overrides[get_current_user] = lambda: admin_session(is_superuser=False)
        response = await client.get("/api/v1/auth/menus")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["/admin/dashboard"]["enabled"] is True
        assert "/admin/api-key" not in data
        assert "/admin/playground" not in data  # 需 inference_debug:exec（注入平台 Key 的入口）

    async def test_normal_user_switch_off_still_annotated(self, client: AsyncClient, monkeypatch):
        """非超管路径的开关标注：关掉的菜单仍在返回中标 enabled=False。"""
        monkeypatch.setattr("llmpod.services.menu_service.get_user_permissions", _only_dashboard_perms)
        monkeypatch.setattr("llmpod.services.menu_service.get_user_disabled_menus", _dashboard_user_disabled)
        app.dependency_overrides[get_current_user] = lambda: admin_session(is_superuser=False)
        response = await client.get("/api/v1/auth/menus")
        assert response.status_code == 200
        assert response.json()["data"]["/admin/dashboard"]["enabled"] is False

    async def test_tenant_scope_overlays_user_switch(self, client: AsyncClient, monkeypatch):
        """租户域下发叠加本人开关：被下线的菜单标 enabled=False（仍下发）。"""
        monkeypatch.setattr(
            "llmpod.services.menu_service.get_user_disabled_menus",
            _playground_user_disabled,
        )
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        response = await client.get("/api/v1/auth/menus")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["/tenant/playground"]["enabled"] is False
        assert data["/tenant/console"]["enabled"] is True


class TestAuthMenusUpdate:
    """/auth/menus/update：自助全量覆盖写，只作用于当前会话用户。"""

    async def test_requires_login(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/menus/update", json={"disabled": []})
        assert response.status_code == 401
        assert response.json()["error_code"] == ErrorCode.UNAUTHENTICATED.value

    async def test_tenant_user_self_scope(self, client: AsyncClient, monkeypatch):
        """租户账号自助：user_id/scope 取会话，body 里塞的 user_id 被忽略。"""
        captured = {}

        async def fake_set(db, *, user_id, scope, paths, updated_by):
            captured.update(user_id=user_id, scope=scope, paths=paths, updated_by=updated_by)
            return frozenset(paths)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.set_user_disabled_menus", fake_set)
        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        response = await client.post(
            "/api/v1/auth/menus/update",
            json={"user_id": 999, "disabled": ["/tenant/console"]},
        )
        assert response.status_code == 200
        assert captured == {
            "user_id": 9,  # 取会话用户，非 body 的 999
            "scope": "tenant",
            "paths": ["/tenant/console"],
            "updated_by": 9,
        }
        assert response.json()["data"] == {
            "user_id": 9,
            "disabled": ["/tenant/console"],
        }

    async def test_platform_user_self_scope(self, client: AsyncClient, monkeypatch):
        """平台账号自助：scope=platform，改的是 platform 域菜单。"""
        captured = {}

        async def fake_set(db, *, user_id, scope, paths, updated_by):
            captured.update(user_id=user_id, scope=scope, paths=paths, updated_by=updated_by)
            return frozenset(paths)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.set_user_disabled_menus", fake_set)
        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.post("/api/v1/auth/menus/update", json={"disabled": ["/admin/dashboard"]})
        assert response.status_code == 200
        assert captured == {
            "user_id": 1,
            "scope": "platform",
            "paths": ["/admin/dashboard"],
            "updated_by": 1,
        }

    async def test_platform_user_audit_records_platform_tenant(self, client: AsyncClient, monkeypatch):
        """平台账号自助改菜单：审计归属记平台租户 0（本人数据），不退化成「全局」。"""
        entries = []

        async def _capture(entry):
            entries.append(entry)

        async def fake_set(db, *, user_id, scope, paths, updated_by):
            return frozenset(paths)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.set_user_disabled_menus", fake_set)
        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _capture)
        app.dependency_overrides[get_current_user] = lambda: admin_session()

        response = await client.post("/api/v1/auth/menus/update", json={"disabled": ["/admin/dashboard"]})
        assert response.status_code == 200
        assert len(entries) == 1
        assert entries[0].resource_type == "menus"
        assert entries[0].tenant_id == PLATFORM_TENANT_ID

    async def test_update_empty_list_means_all_enabled(self, client: AsyncClient, monkeypatch):
        captured = {}

        async def fake_set(db, *, user_id, scope, paths, updated_by):
            captured["paths"] = paths
            return frozenset(paths)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.set_user_disabled_menus", fake_set)
        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        response = await client.post("/api/v1/auth/menus/update", json={"disabled": []})
        assert response.status_code == 200
        assert captured["paths"] == []
        assert response.json()["data"]["disabled"] == []

    async def test_update_unknown_path_maps_to_422(self, client: AsyncClient, monkeypatch):
        """service 对未声明路径 raise LLMPodValidationError → 统一 422 Result。"""

        async def fake_set(db, **_kw):
            raise LLMPodValidationError("menus.disabled 含未声明的菜单路径：/tenant/nope")

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.set_user_disabled_menus", fake_set)
        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)
        app.dependency_overrides[get_current_user] = lambda: tenant_session()

        response = await client.post("/api/v1/auth/menus/update", json={"disabled": ["/tenant/nope"]})
        assert response.status_code == 422
        assert response.json()["error_code"] == ErrorCode.VALIDATION_ERROR.value
