"""审计日志集成测试（stub 驱动，不依赖真实 PG/Redis）。

沿用项目"stub get_db + 鉴权覆盖 + service 桩"原则：

- 查询 API：admin 全量 / tenant 强制租户隔离（monkeypatch list_audit_logs 断言 tenant_id 传入）。
- 权限门：无 token → 401；有 token 无权限 → 403 且落审计。
- 登录失败：未知用户名不落账（防匿名刷 audit_log）；注册用户失败落真实主体。
"""

from __future__ import annotations

from types import SimpleNamespace

from llmpod.app import app
from llmpod.apps.webserver.auth.deps import get_current_user
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.core.exceptions import AuthError, ErrorCode
from llmpod.models.audit_log import AuditLog
from llmpod.services import audit_service


def _tenant_user() -> SessionData:
    """租户会话（作 get_current_user 依赖覆盖，签名须无参，避免被 FastAPI 当查询参数）。"""
    return SessionData(
        user_id=9,
        username="tenant_user",
        tenant_id=5,
        access_domain="tenant",
        role_codes=["tenant_admin"],
        role_ids=[1],
        is_superuser=False,
    )


def _platform_admin() -> SessionData:
    """平台超管会话（admin 路由依赖覆盖；is_superuser 短路权限校验）。"""
    return SessionData(
        user_id=1,
        username="admin",
        tenant_id=0,
        access_domain="platform",
        role_codes=["super_admin"],
        role_ids=[1],
        is_superuser=True,
    )


def _platform_operator() -> SessionData:
    """平台非超管会话：过 require_platform_role 外层门禁，但细粒度权限由 user_has_permission 决定。"""
    return SessionData(
        user_id=8,
        username="operator",
        tenant_id=0,
        access_domain="platform",
        role_codes=["operator"],
        role_ids=[2],
        is_superuser=False,
    )


class RecordingSession:
    """独立 session 桩：捕获 add 的 AuditLog。"""

    def __init__(self, records):
        self._records = records

    def add(self, obj):
        self._records.append(obj)

    async def commit(self):
        pass

    async def close(self):
        pass


class TestAuditLogQueryRoutes:
    """查询 API 权限门：无 token 401 / 有 token 走权限校验。"""

    async def test_admin_route_401_without_token(self, client):
        response = await client.get("/api/v1/admin/audit-logs")
        assert response.status_code == 401

    async def test_tenant_route_401_without_token(self, client):
        response = await client.get("/api/v1/tenant/audit-logs")
        assert response.status_code == 401

    async def test_tenant_route_forces_tenant_scope(self, client, monkeypatch):
        """tenant 查询强制 tenant_id = current_tenant_id（唯一可信来源），不信任查询参数。"""
        captured = {}

        async def fake_list(db, *, page, page_size, tenant_id, **kw):
            captured["tenant_id"] = tenant_id
            return [], 0

        monkeypatch.setattr("llmpod.apps.webserver.routes.tenant.audit_logs.list_audit_logs", fake_list)

        # 需要权限放行：stub user_has_permission → True
        async def _allow(*a, **k):
            return True

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _allow)
        app.dependency_overrides[get_current_user] = _tenant_user

        response = await client.get("/api/v1/tenant/audit-logs")
        assert response.status_code == 200
        assert captured["tenant_id"] == 5  # 强制本租户

    async def test_admin_route_passes_tenant_filter(self, client, monkeypatch):
        """admin 查询显式传 tenant_id → 精确过滤该租户（不看平台域）。"""
        captured = {}

        async def fake_list(db, *, page, page_size, tenant_id, **kw):
            captured["tenant_id"] = tenant_id
            captured["exclude_tenant_scope"] = kw.get("exclude_tenant_scope")
            return [], 0

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.audit_logs.list_audit_logs", fake_list)

        async def _allow(*a, **k):
            return True

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _allow)
        app.dependency_overrides[get_current_user] = _platform_admin

        response = await client.get("/api/v1/admin/audit-logs", params={"tenant_id": 7})
        assert response.status_code == 200
        assert captured["tenant_id"] == 7
        assert captured["exclude_tenant_scope"] is False

    async def test_admin_route_default_excludes_tenant_rows(self, client, monkeypatch):
        """admin 查询不传 tenant_id → 默认只看平台域操作（exclude_tenant_scope=True）。"""
        captured = {}

        async def fake_list(db, *, page, page_size, tenant_id, **kw):
            captured["tenant_id"] = tenant_id
            captured["exclude_tenant_scope"] = kw.get("exclude_tenant_scope")
            return [], 0

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.audit_logs.list_audit_logs", fake_list)

        async def _allow(*a, **k):
            return True

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _allow)
        app.dependency_overrides[get_current_user] = _platform_admin

        response = await client.get("/api/v1/admin/audit-logs")
        assert response.status_code == 200
        assert captured["tenant_id"] is None
        assert captured["exclude_tenant_scope"] is True

    async def test_admin_route_passes_tenant_zero(self, client, monkeypatch):
        """传 tenant_id=0 → 原样透传 + 置位平台收敛开关（service 层把 0 解作平台域）。"""
        captured = {}

        async def fake_list(db, *, page, page_size, tenant_id, **kw):
            captured["tenant_id"] = tenant_id
            captured["exclude_tenant_scope"] = kw.get("exclude_tenant_scope")
            return [], 0

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.audit_logs.list_audit_logs", fake_list)

        async def _allow(*a, **k):
            return True

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _allow)
        app.dependency_overrides[get_current_user] = _platform_admin

        response = await client.get("/api/v1/admin/audit-logs", params={"tenant_id": 0})
        assert response.status_code == 200
        assert captured["tenant_id"] == 0
        assert captured["exclude_tenant_scope"] is True


class TestAuditLogLabelFields:
    """原字段中文翻译：命中译中文 / 未知回退英文 / 空资源透传 / 筛选入参仍英文。"""

    @staticmethod
    def _row(action, resource_type):
        """全字段 fake 行（SimpleNamespace 走 from_attributes）。"""
        return SimpleNamespace(
            id=1,
            actor_type="user",
            actor_id=8,
            actor_name="operator",
            tenant_id=0,
            action=action,
            result="success",
            resource_type=resource_type,
            resource_id="1",
            detail={},
            ip_address=None,
            created_at="2026-10-24T00:00:00Z",
        )

    async def test_admin_route_translates_fields(self, client, monkeypatch):
        """admin 列表：action/resource_type 出参为中文，未知代码回退英文原串，
        resource_type=None 透传 None；筛选入参原样传英文代码（DB 等值过滤）。"""
        rows = [
            self._row("upgrade_batch", "node"),
            self._row("wat", "mystery"),
            self._row("bootstrap", None),
        ]
        captured = {}

        async def fake_list(db, *, action=None, **kw):
            captured["action"] = action
            return rows, len(rows)

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.audit_logs.list_audit_logs", fake_list)

        async def _allow(*a, **k):
            return True

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _allow)
        app.dependency_overrides[get_current_user] = _platform_admin

        response = await client.get("/api/v1/admin/audit-logs", params={"action": "upgrade_batch"})
        assert response.status_code == 200
        items = response.json()["data"]["items"]
        # 命中：出参译为中文
        assert items[0]["action"] == "批量升级"
        assert items[0]["resource_type"] == "节点"
        # 未知代码：回退英文原串
        assert items[1]["action"] == "wat"
        assert items[1]["resource_type"] == "mystery"
        # 空资源：透传 None
        assert items[2]["action"] == "节点引导"
        assert items[2]["resource_type"] is None
        # 筛选入参仍是英文代码（后端按 DB 存储值等值过滤）
        assert captured["action"] == "upgrade_batch"

    async def test_tenant_route_translates_fields(self, client, monkeypatch):
        """tenant 列表：租户形状（AuditLogTenantOut）同样翻译。"""
        rows = [self._row("create", "api_key")]

        async def fake_list(db, **kw):
            return rows, len(rows)

        monkeypatch.setattr("llmpod.apps.webserver.routes.tenant.audit_logs.list_audit_logs", fake_list)

        async def _allow(*a, **k):
            return True

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _allow)
        app.dependency_overrides[get_current_user] = _tenant_user

        response = await client.get("/api/v1/tenant/audit-logs")
        assert response.status_code == 200
        item = response.json()["data"]["items"][0]
        assert item["action"] == "创建"
        assert item["resource_type"] == "API Key"
        # 租户形状照旧不外露 actor_type / tenant_id
        assert "actor_type" not in item
        assert "tenant_id" not in item


class TestPermissionDeniedAudit:
    """权限拒绝（403）在抛 PermissionDenied 前落审计。"""

    async def test_denied_writes_failed_audit(self, client, monkeypatch):
        records = []
        monkeypatch.setattr(audit_service, "_session_factory", lambda: RecordingSession(records))

        # 平台非超管 + 无细粒度权限 → 403（过外层门禁，卡在 require_permission）
        async def _deny(*a, **k):
            return False

        monkeypatch.setattr("llmpod.apps.webserver.auth.deps.user_has_permission", _deny)
        app.dependency_overrides[get_current_user] = _platform_operator

        response = await client.post("/api/v1/admin/clusters/1/delete")
        assert response.status_code == 403

        assert len(records) == 1
        row = records[0]
        assert isinstance(row, AuditLog)
        assert row.result == "failed"
        assert row.action == "delete"
        assert row.resource_type == "cluster"
        assert "permission_denied" in row.detail["error"]
        assert row.actor_id == 8


class TestLoginFailureAudit:
    """登录失败审计只记注册用户：未知用户名不落账（防匿名刷 audit_log），
    密码错/无角色的注册用户落真实主体（actor_id/actor_name/tenant_id）。"""

    async def test_login_failure_unknown_user_skips_audit(self, client, monkeypatch):
        # 用户不存在：authenticate 的 AuthError 不带 subject → 不落审计（防刷库）
        records = []
        monkeypatch.setattr(audit_service, "_session_factory", lambda: RecordingSession(records))

        async def fake_authenticate(db, username, password, settings):
            raise AuthError("Invalid username or password", error_code=ErrorCode.INVALID_CREDENTIALS)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.authenticate", fake_authenticate)

        response = await client.post("/api/v1/auth/login", json={"username": "ghost", "password": "wrong"})
        assert response.status_code == 401
        assert records == []

    async def test_login_failure_registered_user_writes_failed_audit(self, client, monkeypatch):
        # 注册用户密码错：AuthError.subject 带命中的用户 → 落真实主体的失败审计
        records = []
        monkeypatch.setattr(audit_service, "_session_factory", lambda: RecordingSession(records))

        registered = SimpleNamespace(id=7, username="alice", tenant_id=3)

        async def fake_authenticate(db, username, password, settings):
            raise AuthError(
                "Invalid username or password",
                error_code=ErrorCode.INVALID_CREDENTIALS,
                subject=registered,
            )

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.authenticate", fake_authenticate)

        response = await client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong"})
        assert response.status_code == 401

        assert len(records) == 1
        row = records[0]
        assert row.action == "login"
        assert row.result == "failed"
        assert row.actor_id == 7
        assert row.actor_name == "alice"
        assert row.tenant_id == 3
        assert row.detail["error"] == ErrorCode.INVALID_CREDENTIALS.value
