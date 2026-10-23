"""平台用户管理与租户管理路由集成测试（/api/v1/admin/users、/api/v1/admin/tenants）。

覆盖 routes/admin/users.py 与 routes/admin/tenants.py：
- 平台用户：列表（固定 tenant_id=0 作用域 + 分页）、创建（全局唯一 username 冲突
  兜底 409 / 平台域角色校验 / 去重）、详情 404、更新（改密/状态/非法值）、删除
  （最后管理员守卫 400 + 级联清理）；
- 租户：列表/概览、创建（seed 内置角色 → 引导租户超管 → 可选模型封禁）、更新
  （平台租户禁禁用 / 封禁路由全量替换）、删除（平台租户守卫 + 全量级联）。

桩惯例：OpSession 接管 get_db；超管会话短路权限门；hash_password、enrich_tenants、
seed_tenant_builtin_roles 按项目惯例在路由/seed 模块命名空间打桩；审计 no-op。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from llmpod.app import app
from llmpod.config.constants import PLATFORM_TENANT_ID
from llmpod.db import get_db
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.models.tenant import Tenant
from llmpod.models.tenant_model_block import TenantModelBlock
from llmpod.models.user import User
from llmpod.models.user_role import UserRole
from llmpod.models.wallet import Wallet

from _fakes import PROFILE_MAX_CASES, TENANT_ADMIN_PROFILE_MAX_CASES, admin_session
from _op_session import OpSession

TS = datetime(2026, 1, 1, tzinfo=timezone.utc)

USERS = "/api/v1/admin/users"
TENANTS = "/api/v1/admin/tenants"


def _user(uid: int, tenant_id: int, username: str, status: str = "active", email: str | None = None) -> User:
    return User(
        id=uid,
        tenant_id=tenant_id,
        username=username,
        password_hash="$2b$12$old",
        email=email,
        status=status,
        created_at=TS,
        updated_at=TS,
    )


def _admin_binding(user_id: int, role_id: int) -> UserRole:
    """注入 ``code`` 属性补偿 FakeSession 不支持 join（assert_not_last_admin 需读 Role.code）。"""
    ur = UserRole(user_id=user_id, role_id=role_id)
    ur.code = "super_admin"
    return ur


def _base_session() -> OpSession:
    s = OpSession()
    # 平台域用户 1/2 + 租户域用户 9（越界样本）；用户 2 持平台 super_admin
    s.preload(_user(1, 0, "root"))
    s.preload(_user(2, 0, "alice"))
    s.preload(_user(9, 5, "tenant-owner"))
    s.preload(Role(id=11, tenant_id=0, name="super", code="super_admin", is_builtin=True))
    s.preload(Role(id=13, tenant_id=5, name="tenant-role", code=None, is_builtin=False))
    s.preload(_admin_binding(2, 11))
    s.preload(Wallet(id=1, tenant_id=0, balance=Decimal("50"), frozen_balance=Decimal("10")))
    # 租户：T1（active）、平台租户 0
    s.preload(Tenant(id=1, name="T1", status="active", created_at=TS, updated_at=TS))
    s.preload(Tenant(id=5, name="T5", status="disabled", created_at=TS, updated_at=TS))
    return s


@pytest.fixture
async def session():
    s = _base_session()

    async def _override():
        yield s

    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[get_current_user] = lambda: admin_session()
    yield s
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _fast_hash_and_mute_audit(monkeypatch):
    monkeypatch.setattr("llmpod.apps.webserver.routes.admin.users.hash_password", lambda p: f"$2b$12${p}")
    monkeypatch.setattr("llmpod.apps.webserver.routes.admin.tenants.hash_password", lambda p: f"$2b$12${p}")

    async def _no_audit(_entry):
        return None

    monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)


from llmpod.apps.webserver.auth.deps import get_current_user  # noqa: E402 — fixture 定义后才 import，保持就近


def _flush_boom(s: OpSession, constraint: str | None = None):
    """把 flush 换成抛 IntegrityError 的桩；constraint 给约束名时模拟 asyncpg 形态
    （约束名在 orig.__cause__ 上，orig 本身无 diag——分诊函数两处都会取）。"""

    orig = Exception("uq")
    if constraint is not None:
        # __cause__ 必须是异常（asyncpg 抛 UniqueViolationError 被 SQLAlchemy 包一层）
        cause = Exception(constraint)
        cause.constraint_name = constraint  # type: ignore[attr-defined]
        orig.__cause__ = cause

    async def _raise():
        raise IntegrityError("INSERT ...", {}, orig)

    s.flush = _raise  # type: ignore[method-assign]


class TestAdminUserList:
    async def test_list_scoped_paginated_with_enrichment(self, client: AsyncClient, session):
        resp = await client.get(USERS)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert [u["id"] for u in data["items"]] == [2, 1]  # order_by id desc
        alice = data["items"][0]
        assert alice["role_ids"] == [11]
        assert Decimal(alice["tenant_balance"]) == Decimal("50")
        assert Decimal(alice["tenant_available"]) == Decimal("40")

    async def test_list_page2(self, client: AsyncClient, session):
        resp = await client.get(USERS, params={"page": 2, "page_size": 1})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_pages"] == 2
        assert [u["id"] for u in data["items"]] == [1]


class TestAdminUserCreate:
    async def test_create_with_platform_roles(self, client: AsyncClient, session):
        resp = await client.post(
            USERS,
            json={"username": "bob", "password": "secret1", "email": "b@x.y", "role_ids": [11, 11]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["tenant_id"] == 0  # 固定归属平台租户
        assert data["role_ids"] == [11]  # 重复 role_id 去重写入
        created = next(u for u in session.all(User) if u.username == "bob")
        assert created.password_hash == "$2b$12$secret1"

    async def test_create_rejects_non_platform_role(self, client: AsyncClient, session):
        resp = await client.post(USERS, json={"username": "carol", "password": "secret1", "role_ids": [13]})
        assert resp.status_code == 409
        assert "非平台域" in resp.json()["message"]

    async def test_create_username_conflict_409(self, client: AsyncClient, session):
        """裸 IntegrityError（无约束名可辨）→ 按用户名口径兜底 409。"""
        _flush_boom(session)
        resp = await client.post(USERS, json={"username": "x", "password": "p", "role_ids": []})
        assert resp.status_code == 409
        assert resp.json()["message"] == "该用户名不可用"
        assert resp.json()["error_code"] == "CONFLICT"

    async def test_create_email_conflict_named_409(self, client: AsyncClient, session):
        """撞 uq_user_tenant_email（asyncpg 带 orig.diag.constraint_name）→ 分诊报邮箱，
        不再误报成用户名冲突（同一条 INSERT 带两条唯一约束）。"""
        _flush_boom(session, constraint="uq_user_tenant_email")
        resp = await client.post(USERS, json={"username": "x", "password": "p", "role_ids": []})
        assert resp.status_code == 409
        assert resp.json()["message"] == "该邮箱在本租户内已被使用"
        assert resp.json()["error_code"] == "CONFLICT"

    async def test_create_username_constraint_named_409(self, client: AsyncClient, session):
        """撞 uq_user_username（带约束名）→ 用户名口径。"""
        _flush_boom(session, constraint="uq_user_username")
        resp = await client.post(USERS, json={"username": "x", "password": "p", "role_ids": []})
        assert resp.status_code == 409
        assert resp.json()["message"] == "该用户名不可用"

    @pytest.mark.parametrize("field,max_length", PROFILE_MAX_CASES)
    async def test_create_rejects_over_long_profile_strings(self, client: AsyncClient, session, field, max_length):
        """手机号/邮箱超列宽（user.phone = String(32)、user.email = String(128)）须拦在请求模型。

        超长字符串写列会抛 asyncpg StringDataRightTruncationError，与越界数字同一条链路：
        被 asyncpg 方言塌成通用 DBAPIError，躲过 IntegrityError 处理器 → 从前是裸 500。
        """
        payload = {
            "username": "dave",
            "password": "secret1",
            "role_ids": [],
            field: "x" * (max_length + 1),
        }
        resp = await client.post(USERS, json=payload)

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

        # 上界本身（= 列宽）必须仍然合法，防收得过紧
        resp = await client.post(USERS, json={**payload, field: "x" * max_length})
        assert resp.status_code == 200


class TestAdminUserGetUpdateDelete:
    async def test_get_200_and_404(self, client: AsyncClient, session):
        resp = await client.get(f"{USERS}/1")
        assert resp.status_code == 200
        assert resp.json()["data"]["username"] == "root"

    async def test_get_tenant_user_404(self, client: AsyncClient, session):
        """管理员端只管 tenant_id=0 的用户：租户用户不可见。"""
        resp = await client.get(f"{USERS}/9")
        assert resp.status_code == 404

    async def test_update_resets_password_and_status(self, client: AsyncClient, session):
        resp = await client.post(
            f"{USERS}/1/update", json={"password": "newpass1", "status": "disabled", "phone": "120"}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "disabled"
        row = await session.get(User, 1)
        assert row.password_hash == "$2b$12$newpass1"

    async def test_update_omitted_email_clears(self, client: AsyncClient, session):
        """不传 email/phone = 清空（前端清空输入框时把键整个丢掉，不发空串也不发 null）。

        修复前会静默保留旧值。注意本 payload 与列表页启停发出的完全相同——代价见路由更新处注释。
        """
        session.preload(_user(3, 0, "carol", email="carol@x.y"))

        resp = await client.post(f"{USERS}/3/update", json={"status": "disabled"})

        assert resp.status_code == 200
        row = await session.get(User, 3)
        assert row.email is None
        assert row.status == "disabled"

    async def test_update_invalid_status_422(self, client: AsyncClient, session):
        resp = await client.post(f"{USERS}/1/update", json={"status": "frozen"})
        assert resp.status_code == 422

    async def test_update_empty_body_422(self, client: AsyncClient, session):
        """本端空请求仍拒（编辑表单必带 status）；tenant 端不拒，见两处路由注释。"""
        resp = await client.post(f"{USERS}/1/update", json={})
        assert resp.status_code == 422

    async def test_update_missing_404(self, client: AsyncClient, session):
        resp = await client.post(f"{USERS}/999/update", json={"status": "disabled"})
        assert resp.status_code == 404

    async def test_update_email_conflict_409(self, client: AsyncClient, session):
        _flush_boom(session)
        resp = await client.post(f"{USERS}/1/update", json={"email": "dup@x.y"})
        assert resp.status_code == 409

    async def test_delete_cascades_bindings(self, client: AsyncClient, session):
        session.preload(UserRole(user_id=2, role_id=11))  # 冗余绑定行一并清理
        resp = await client.post(f"{USERS}/2/delete")
        assert resp.status_code == 200
        assert resp.json()["data"] == {"id": 2, "deleted": True}
        assert await session.get(User, 2) is None
        assert not [ur for ur in session.all(UserRole) if ur.user_id == 2]

    async def test_delete_last_super_admin_refused(self, client: AsyncClient, session):
        """alice 是 super_admin 最后一名持有者 → 删除被拒。"""
        resp = await client.post(f"{USERS}/2/delete")
        assert resp.status_code == 400
        assert "至少需保留一个管理员" in resp.json()["message"]

    async def test_delete_missing_404(self, client: AsyncClient, session):
        resp = await client.post(f"{USERS}/999/delete")
        assert resp.status_code == 404

    @pytest.mark.parametrize("field,max_length", PROFILE_MAX_CASES)
    async def test_update_rejects_over_long_profile_strings(self, client: AsyncClient, session, field, max_length):
        """更新路径的 phone/email 指向同一批列，同样须拦在请求模型（成因见创建侧说明）。"""
        resp = await client.post(f"{USERS}/1/update", json={field: "x" * (max_length + 1)})

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

        assert (await client.post(f"{USERS}/1/update", json={field: "x" * max_length})).status_code == 200


class TestAdminUserAuditAttribution:
    """平台用户管理的审计行归属 = 平台租户 0（资源自身带租户，不再落 NULL）。

    本文件 autouse fixture 会静音审计，故此处重新接管 ``_write_audit`` 抓行。
    """

    async def test_update_platform_user_records_platform_tenant(self, client: AsyncClient, session, monkeypatch):
        entries = []

        async def _capture(entry):
            entries.append(entry)

        monkeypatch.setattr("llmpod.services.audit_service._write_audit", _capture)

        resp = await client.post(f"{USERS}/1/update", json={"phone": "120"})
        assert resp.status_code == 200
        assert len(entries) == 1
        assert entries[0].resource_type == "user"
        assert entries[0].tenant_id == PLATFORM_TENANT_ID


class TestTenantListOverview:
    async def test_list_with_status_filter(self, client: AsyncClient, session, monkeypatch):
        async def _fake_enrich(db, tenants):
            for t in tenants:
                t.user_count, t.role_count = 1, 2
                t.balance = t.frozen_balance = Decimal("0")
                t.available = Decimal("0")
                t.blocked_models = []
            return tenants

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.tenants.enrich_tenants", _fake_enrich)
        resp = await client.get(TENANTS, params={"status": "active"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1  # 只有 T1 是 active
        assert [t["id"] for t in data["items"]] == [1]
        assert data["items"][0]["user_count"] == 1

    async def test_overview_aggregate(self, client: AsyncClient, session, monkeypatch):
        async def _fake_overview(db, *, days, top_n):
            return {
                "total_tenants": 2,
                "active_tenants": 1,
                "disabled_tenants": 1,
                "total_users": 1,
                "dates": ["2026-01-01"],
                "consume_trend": [{"tenant_id": 1, "tenant_name": "T1", "data": [3.5]}],
                "balance_top": [{"tenant_id": 1, "tenant_name": "T1", "balance": "10", "available": "8"}],
            }

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.tenants.tenant_overview", _fake_overview)
        resp = await client.get(f"{TENANTS}/overview", params={"days": 7, "top_n": 3})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_tenants"] == 2
        assert data["consume_trend"][0]["data"] == [3.5]
        assert Decimal(data["balance_top"][0]["available"]) == Decimal("8")


class TestTenantCreate:
    async def _seed_roles(self, s: OpSession):
        """seed 桩：真实落内置角色（路由随后按 code 查 tenant_admin 引导超管）。"""
        from llmpod.permission.seed import TENANT_BUILTIN_ROLES

        async def _seed(db, tenant_id):
            for code, name in TENANT_BUILTIN_ROLES:
                db.add(Role(tenant_id=tenant_id, name=name, code=code, is_builtin=True))
            await db.flush()

        return _seed

    async def test_create_seeds_roles_and_bootstrap_admin(self, client: AsyncClient, session, monkeypatch):
        monkeypatch.setattr("llmpod.permission.seed.seed_tenant_builtin_roles", await self._seed_roles(session))

        async def _fake_enrich(db, tenants):
            return tenants

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.tenants.enrich_tenants", _fake_enrich)

        resp = await client.post(
            TENANTS,
            json={
                "name": "Acme",
                "admin_username": "acme-admin",
                "admin_password": "secret1",
                "blocked_route_ids": [7, 8],
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        tenant_id = data["id"]
        assert data["name"] == "Acme"
        # 内置角色已 seed
        codes = {r.code for r in session.all(Role) if r.tenant_id == tenant_id}
        assert "tenant_admin" in codes
        # 超管已引导并绑定 tenant_admin
        admin = next(u for u in session.all(User) if u.username == "acme-admin")
        assert admin.tenant_id == tenant_id
        admin_role = next(r for r in session.all(Role) if r.tenant_id == tenant_id and r.code == "tenant_admin")
        assert await session.get(UserRole, (admin.id, admin_role.id)) is not None
        # 模型封禁行已写
        blocked = {b.route_id for b in session.all(TenantModelBlock) if b.tenant_id == tenant_id}
        assert blocked == {7, 8}

    @pytest.mark.parametrize("field,max_length", TENANT_ADMIN_PROFILE_MAX_CASES)
    async def test_create_rejects_over_long_admin_profile_strings(
        self, client: AsyncClient, session, monkeypatch, field, max_length
    ):
        """建租户时引导的超管也写 user 表的 phone/email 两列，同样须拦在请求模型。"""
        monkeypatch.setattr("llmpod.permission.seed.seed_tenant_builtin_roles", await self._seed_roles(session))

        async def _fake_enrich(db, tenants):
            return tenants

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.tenants.enrich_tenants", _fake_enrich)

        payload = {
            "name": "Acme",
            "admin_username": "acme-admin",
            "admin_password": "secret1",
            field: "x" * (max_length + 1),
        }
        resp = await client.post(TENANTS, json=payload)

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

        # 上界本身（= 列宽）必须仍然合法，防收得过紧
        resp = await client.post(TENANTS, json={**payload, field: "x" * max_length})
        assert resp.status_code == 200

    async def test_create_name_conflict_409(self, client: AsyncClient, session, monkeypatch):
        _flush_boom(session)
        resp = await client.post(TENANTS, json={"name": "Dup", "admin_username": "a", "admin_password": "p"})
        assert resp.status_code == 409
        assert resp.json()["message"] == "该租户名已存在"
        assert resp.json()["error_code"] == "CONFLICT"

    async def test_create_admin_username_conflict_409(self, client: AsyncClient, session, monkeypatch):
        """租户行创建成功但超管用户名全局冲突 → 整体 409。"""
        real_flush = session.flush

        calls = {"n": 0}

        async def _third_flush_boom():
            calls["n"] += 1
            # 依次为：1=租户行、2=seed 内置角色、3=admin 用户行（在此冲突）
            if calls["n"] >= 3:
                raise IntegrityError("INSERT ...", {}, Exception("uq_user_username"))
            await real_flush()

        session.flush = _third_flush_boom  # type: ignore[method-assign]
        monkeypatch.setattr("llmpod.permission.seed.seed_tenant_builtin_roles", await self._seed_roles(session))
        resp = await client.post(TENANTS, json={"name": "Acme2", "admin_username": "acme-admin", "admin_password": "p"})
        assert resp.status_code == 409
        assert resp.json()["message"] == "管理员用户名或邮箱已存在"
        assert resp.json()["error_code"] == "CONFLICT"

    async def test_create_without_seeded_role_400(self, client: AsyncClient, session, monkeypatch):
        """seed 未建内置角色（异常场景）→ 无法引导超管，400。"""

        async def _noop(db, tenant_id):
            return None

        monkeypatch.setattr("llmpod.permission.seed.seed_tenant_builtin_roles", _noop)
        resp = await client.post(TENANTS, json={"name": "Broken", "admin_username": "b1", "admin_password": "p"})
        assert resp.status_code == 400
        assert "tenant_admin" in resp.json()["message"]


class TestTenantGetUpdateDelete:
    async def test_get_200(self, client: AsyncClient, session, monkeypatch):
        async def _fake_enrich(db, tenants):
            return tenants

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.tenants.enrich_tenants", _fake_enrich)
        resp = await client.get(f"{TENANTS}/1")
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "T1"

    async def test_get_404(self, client: AsyncClient, session):
        resp = await client.get(f"{TENANTS}/999")
        assert resp.status_code == 404

    async def test_update_name_and_blocked_routes(self, client: AsyncClient, session, monkeypatch):
        async def _fake_enrich(db, tenants):
            return tenants

        monkeypatch.setattr("llmpod.apps.webserver.routes.admin.tenants.enrich_tenants", _fake_enrich)
        session.preload(TenantModelBlock(id=31, tenant_id=1, route_id=7, status="active"))
        resp = await client.post(f"{TENANTS}/1/update", json={"name": "T1-new", "blocked_route_ids": [9]})
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "T1-new"
        # 封禁全量替换（PUT 语义）：不在 desired 集的旧行被删、新集 active
        blocks = {b.route_id: b.status for b in session.all(TenantModelBlock) if b.tenant_id == 1}
        assert 7 not in blocks
        assert blocks[9] == "active"

    async def test_update_empty_body_422(self, client: AsyncClient, session):
        resp = await client.post(f"{TENANTS}/1/update", json={})
        assert resp.status_code == 422

    async def test_update_platform_tenant_disable_refused(self, client: AsyncClient, session):
        resp = await client.post(f"{TENANTS}/0/update", json={"status": "disabled"})
        assert resp.status_code == 400
        assert "不可禁用" in resp.json()["message"]

    async def test_update_missing_404(self, client: AsyncClient, session):
        resp = await client.post(f"{TENANTS}/999/update", json={"name": "X"})
        assert resp.status_code == 404

    async def test_update_name_conflict_409(self, client: AsyncClient, session):
        _flush_boom(session)
        resp = await client.post(f"{TENANTS}/1/update", json={"name": "Dup"})
        assert resp.status_code == 409

    async def test_delete_platform_tenant_refused(self, client: AsyncClient, session):
        resp = await client.post(f"{TENANTS}/0/delete")
        assert resp.status_code == 400
        assert "不可删除" in resp.json()["message"]

    async def test_delete_missing_404(self, client: AsyncClient, session):
        resp = await client.post(f"{TENANTS}/999/delete")
        assert resp.status_code == 404

    async def test_delete_cascades_users_roles_and_blocks(self, client: AsyncClient, session):
        """删除租户级联清理：用户、角色绑定、角色授权、封禁行（零物理 FK 全手工清）。"""
        s = session
        t = Tenant(id=7, name="T7", status="active", created_at=TS, updated_at=TS)
        s.preload(t)
        s.preload(_user(21, 7, "t7-admin"))
        s.preload(_user(22, 7, "t7-op"))
        s.preload(Role(id=41, tenant_id=7, name="r", code="tenant_admin", is_builtin=True))
        s.preload(Role(id=42, tenant_id=0, name="platform-role", code=None, is_builtin=False))
        s.preload(RolePermission(role_id=41, permission_id=101))
        s.preload(UserRole(user_id=21, role_id=41))
        s.preload(UserRole(user_id=22, role_id=42))  # 跨租户脏绑定，也须被清
        s.preload(TenantModelBlock(id=51, tenant_id=7, route_id=7, status="active"))

        resp = await client.post(f"{TENANTS}/7/delete")
        assert resp.status_code == 200
        assert resp.json()["data"] == {"id": 7, "deleted": True}
        assert await s.get(Tenant, 7) is None
        assert not [u for u in s.all(User) if u.tenant_id == 7]
        assert not [r for r in s.all(Role) if r.tenant_id == 7]
        assert not [rp for rp in s.all(RolePermission) if rp.role_id == 41]
        assert not [ur for ur in s.all(UserRole) if ur.user_id in (21, 22) or ur.role_id == 41]
        assert not [b for b in s.all(TenantModelBlock) if b.tenant_id == 7]
