"""租户用户管理路由集成测试（/api/v1/tenant/users/*，stub 驱动）。

覆盖 routes/tenant/users.py：
- 列表/详情：租户作用域 + role_ids / 所属租户余额装配（真实 enrich 走 OpSession）；
- 创建：username 全局唯一冲突兜底（IntegrityError→409）、跨租户角色拒绝、role_ids 去重；
- 更新：改密/邮箱/手机/状态、非法状态 422、空体 422、冲突 409；
- 删除：最后管理员守卫 400、级联清理角色关联、跨租户 404。

桩惯例：OpSession 接管 get_db；tenant_session 短路权限门；hash_password 打桩
（避免真实 bcrypt 慢）；审计落库 no-op。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from llmpod.app import app
from llmpod.db import get_db
from llmpod.models.role import Role
from llmpod.models.user import User
from llmpod.models.user_role import UserRole
from llmpod.models.wallet import Wallet

from _fakes import PROFILE_MAX_CASES, tenant_session
from _op_session import OpSession

TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
TENANT_ID = 5

BASE = "/api/v1/tenant/users"


def _admin_binding(user_id: int, role_id: int = 21) -> UserRole:
    """预置 tenant_admin 绑定行；注入 ``code`` 属性补偿 FakeSession 不支持 join
    （assert_not_last_admin 的 select(UserRole.role_id).join(Role) 需读 Role.code）。"""
    ur = UserRole(user_id=user_id, role_id=role_id)
    ur.code = "tenant_admin"
    return ur


def _user(uid: int, tenant_id: int, username: str, phone: str | None = None) -> User:
    return User(
        id=uid,
        tenant_id=tenant_id,
        username=username,
        password_hash="$2b$12$old",
        email=f"{username}@x.y",
        phone=phone,
        status="active",
        created_at=TS,
        updated_at=TS,
    )


def _base_session() -> OpSession:
    s = OpSession()
    # 本租户用户 9/10；平台域用户 1（越界样本）
    s.preload(_user(9, TENANT_ID, "owner"))
    s.preload(_user(10, TENANT_ID, "member", phone="13800000000"))
    s.preload(_user(1, 0, "platform"))
    # 角色：21 本租户内置 tenant_admin、22 本租户自定义、23 平台域（越界样本）
    s.preload(Role(id=21, tenant_id=TENANT_ID, name="租户管理员", code="tenant_admin", is_builtin=True))
    s.preload(Role(id=22, tenant_id=TENANT_ID, name="operator", code=None, is_builtin=False))
    s.preload(Role(id=23, tenant_id=0, name="platform-role", code=None, is_builtin=False))
    # user 9 是 tenant_admin（删除守卫用例；code 注入见 _admin_binding）
    s.preload(_admin_binding(9))
    # 租户钱包（余额三段装配用）
    s.preload(Wallet(id=1, tenant_id=TENANT_ID, balance=Decimal("100.5"), frozen_balance=Decimal("20.25")))
    return s


@pytest.fixture
async def session():
    s = _base_session()

    async def _override():
        yield s

    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[get_current_user] = lambda: tenant_session(tenant_id=TENANT_ID, user_id=9)
    yield s
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _fast_hash_and_mute_audit(monkeypatch):
    """路由内 hash_password 打桩（避免真实 bcrypt），审计落库 no-op。"""
    monkeypatch.setattr("llmpod.apps.webserver.routes.tenant.users.hash_password", lambda p: f"$2b$12${p}")

    async def _no_audit(_entry):
        return None

    monkeypatch.setattr("llmpod.services.audit_service._write_audit", _no_audit)


from llmpod.apps.webserver.auth.deps import get_current_user  # noqa: E402 — fixture 定义后才 import，保持就近


class TestListAndGet:
    async def test_list_scoped_with_balance_enrichment(self, client: AsyncClient, session):
        resp = await client.get(BASE)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert {u["id"] for u in data} == {9, 10}  # 平台域用户不进租户列表
        owner = next(u for u in data if u["id"] == 9)
        assert owner["role_ids"] == [21]
        # 钱包三段装配（与钱包路由同口径）
        assert Decimal(owner["tenant_balance"]) == Decimal("100.5")
        assert Decimal(owner["tenant_frozen_balance"]) == Decimal("20.25")
        assert Decimal(owner["tenant_available"]) == Decimal("80.25")

    async def test_get_detail(self, client: AsyncClient, session):
        resp = await client.get(f"{BASE}/10")
        assert resp.status_code == 200
        assert resp.json()["data"]["username"] == "member"
        assert "password_hash" not in resp.json()["data"]

    async def test_get_cross_tenant_404(self, client: AsyncClient, session):
        resp = await client.get(f"{BASE}/1")
        assert resp.status_code == 404

    async def test_get_missing_404(self, client: AsyncClient, session):
        resp = await client.get(f"{BASE}/999")
        assert resp.status_code == 404


class TestCreate:
    async def test_create_with_deduped_tenant_roles(self, client: AsyncClient, session):
        resp = await client.post(
            BASE,
            json={"username": "newbie", "password": "secret1", "email": "n@x.y", "phone": "138", "role_ids": [22, 22]},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["username"] == "newbie"
        assert data["tenant_id"] == TENANT_ID
        assert data["role_ids"] == [22]
        assert "password_hash" not in data  # 明文/哈希均不外泄
        created = next(u for u in session.all(User) if u.username == "newbie")
        assert created.password_hash == "$2b$12$secret1"  # 服务端哈希存储
        assert [ur.role_id for ur in session.all(UserRole) if ur.user_id == created.id] == [22]

    async def test_create_rejects_foreign_tenant_role(self, client: AsyncClient, session):
        resp = await client.post(
            BASE,
            json={"username": "bad", "password": "secret1", "role_ids": [23]},
        )
        assert resp.status_code == 409
        assert "非本租户" in resp.json()["message"]

    @pytest.mark.parametrize("field,max_length", PROFILE_MAX_CASES)
    async def test_create_rejects_over_long_profile_strings(self, client: AsyncClient, session, field, max_length):
        """手机号/邮箱超列宽（user.phone = String(32)、user.email = String(128)）须拦在请求模型。

        超长字符串写列会抛 asyncpg StringDataRightTruncationError，与越界数字同一条链路：
        被 asyncpg 方言塌成通用 DBAPIError，躲过 IntegrityError 处理器 → 从前是裸 500。
        """
        payload = {"username": "dave", "password": "secret1", "role_ids": [], field: "x" * (max_length + 1)}
        resp = await client.post(BASE, json=payload)

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

        # 上界本身（= 列宽）必须仍然合法，防收得过紧
        resp = await client.post(BASE, json={**payload, field: "x" * max_length})
        assert resp.status_code == 200

    async def test_create_username_conflict_409(self, client: AsyncClient, session):
        async def _raise():
            raise IntegrityError("INSERT ...", {}, Exception("uq_user_username"))

        session.flush = _raise  # type: ignore[method-assign]
        resp = await client.post(BASE, json={"username": "any", "password": "secret1", "role_ids": []})
        assert resp.status_code == 409
        assert resp.json()["message"] == "该用户名不可用"
        assert resp.json()["error_code"] == "CONFLICT"

    async def test_create_empty_roles_ok(self, client: AsyncClient, session):
        resp = await client.post(BASE, json={"username": "noroles", "password": "secret1", "role_ids": []})
        assert resp.status_code == 200
        assert resp.json()["data"]["role_ids"] == []


class TestUpdate:
    async def test_update_password_email_phone_status(self, client: AsyncClient, session):
        resp = await client.post(
            f"{BASE}/10/update",
            json={"password": "newpass1", "email": "moved@x.y", "phone": "139", "status": "disabled"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["email"] == "moved@x.y"
        assert data["phone"] == "139"
        assert data["status"] == "disabled"
        row = await session.get(User, 10)
        assert row.password_hash == "$2b$12$newpass1"

    async def test_update_email_empty_clears(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/10/update", json={"email": ""})
        assert resp.status_code == 200
        assert (await session.get(User, 10)).email is None

    async def test_update_omitted_email_clears(self, client: AsyncClient, session):
        """不传 email/phone = 清空（前端清空输入框时把键整个丢掉，不发空串也不发 null）。

        修复前会静默保留旧值。注意本 payload 与列表页启停发出的完全相同——代价见路由更新处注释。
        """
        resp = await client.post(f"{BASE}/10/update", json={"status": "disabled"})

        assert resp.status_code == 200
        row = await session.get(User, 10)
        assert row.email is None
        assert row.phone is None
        assert row.status == "disabled"

    async def test_update_invalid_status_422(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/10/update", json={"status": "frozen"})
        assert resp.status_code == 422

    async def test_update_empty_body_clears_optional_profile_fields(self, client: AsyncClient, session):
        """空请求 = 清空两个可选资料字段（本端更新表单只带 password/email/phone，清空即空请求）。"""
        resp = await client.post(f"{BASE}/10/update", json={})

        assert resp.status_code == 200
        row = await session.get(User, 10)
        assert row.email is None
        assert row.phone is None

    @pytest.mark.parametrize("field,max_length", PROFILE_MAX_CASES)
    async def test_update_rejects_over_long_profile_strings(self, client: AsyncClient, session, field, max_length):
        """更新路径的 phone/email 指向同一批列，同样须拦在请求模型（成因见创建侧说明）。"""
        resp = await client.post(f"{BASE}/10/update", json={field: "x" * (max_length + 1)})

        assert resp.status_code == 422
        body = resp.json()
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [e["loc"] for e in body["data"]] == [["body", field]]

        # 上界本身（= 列宽）必须仍然合法，防收得过紧（用户 10 属本租户，用户 1 是平台域会 404）
        assert (await client.post(f"{BASE}/10/update", json={field: "x" * max_length})).status_code == 200

    async def test_update_cross_tenant_404(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/1/update", json={"status": "disabled"})
        assert resp.status_code == 404

    async def test_update_email_conflict_409(self, client: AsyncClient, session):
        async def _raise():
            raise IntegrityError("UPDATE ...", {}, Exception("uq_user_tenant_email"))

        session.flush = _raise  # type: ignore[method-assign]
        resp = await client.post(f"{BASE}/10/update", json={"email": "dup@x.y"})
        assert resp.status_code == 409


class TestDelete:
    async def test_delete_cascades_role_bindings(self, client: AsyncClient, session):
        session.preload(UserRole(user_id=10, role_id=22))
        resp = await client.post(f"{BASE}/10/delete")
        assert resp.status_code == 200
        assert resp.json()["data"] == {"id": 10, "deleted": True}
        assert await session.get(User, 10) is None
        assert not [ur for ur in session.all(UserRole) if ur.user_id == 10]

    async def test_delete_last_tenant_admin_refused(self, client: AsyncClient, session):
        """user 9 是 tenant_admin 最后一名持有者 → 删除被拒（租户无主守卫）。"""
        resp = await client.post(f"{BASE}/9/delete")
        assert resp.status_code == 400
        assert "至少需保留一个管理员" in resp.json()["message"]
        assert await session.get(User, 9) is not None

    async def test_delete_admin_with_other_holder_ok(self, client: AsyncClient, session):
        """tenant_admin 另有持有者时，删除当前持有者放行。"""
        session.preload(_admin_binding(10))
        resp = await client.post(f"{BASE}/9/delete")
        assert resp.status_code == 200
        assert await session.get(User, 9) is None

    async def test_delete_cross_tenant_404(self, client: AsyncClient, session):
        resp = await client.post(f"{BASE}/1/delete")
        assert resp.status_code == 404
