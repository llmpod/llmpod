"""auth_service.py 单测：登录/会话签发（Redis 桩）/登出/自助改密/访问域与超管判定。

Redis 用手写内存桩（仅 setex/delete，记录调用），经 ``llmpod.db.redis._redis_client``
注入——get_redis() 在调用时读该全局，与生产路径同源。DB 用 ``_minidb.MiniDB``。
"""

from __future__ import annotations

import pytest

from llmpod.config.settings import settings
from llmpod.core.exceptions import AuthError, ErrorCode
from llmpod.core.security import hash_password
from llmpod.db import redis as db_redis
from llmpod.models.role import Role
from llmpod.models.user import User
from llmpod.models.user_role import UserRole
from llmpod.services.auth_service import _determine_access_domain, authenticate, change_password, logout
from tests.unit._minidb import MiniDB

# 模块级算一次真实 bcrypt 哈希（"pw" 的哈希），避免用例内重复百毫秒级 CPU
_PW_HASH = hash_password("pw")


class FakeRedis:
    """auth_service 用到的最小 Redis 替身：setex / delete + 调用记录。"""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.setex_calls: list[tuple[str, int, str]] = []
        self.deleted: list[str] = []

    async def setex(self, key, ttl, value):
        self.strings[key] = value
        self.ttls[key] = ttl
        self.setex_calls.append((key, ttl, value))

    async def delete(self, *keys):
        for k in keys:
            self.deleted.append(k)
            self.strings.pop(k, None)


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(db_redis, "_redis_client", fake)
    return fake


def _mkdb(*, username="alice", password_hash=_PW_HASH, status="active", user_id=100, tenant_id=5, roles=None) -> MiniDB:
    db = MiniDB()
    db.preload(
        User(
            id=user_id,
            tenant_id=tenant_id,
            username=username,
            password_hash=password_hash,
            status=status,
            auth_version=1,
        )
    )
    for role in roles or []:
        db.preload(role)
        db.preload(UserRole(user_id=user_id, role_id=role.id))
    return db


def _role(rid, *, code, tenant_id):
    return Role(id=rid, tenant_id=tenant_id, name=f"r{rid}", code=code, is_builtin=True)


class TestAuthenticate:
    async def test_success_tenant_user(self, redis):
        db = _mkdb(roles=[_role(11, code="tenant_admin", tenant_id=5)])
        result = await authenticate(db, "alice", "pw", settings)
        assert result.token  # 不透明令牌非空
        sess = result.session
        assert sess.user_id == 100 and sess.tenant_id == 5 and sess.username == "alice"
        assert sess.role_ids == [11] and sess.role_codes == ["tenant_admin"]
        assert sess.access_domain == "tenant" and sess.is_superuser is False
        # Redis：以 prefix + hash_token(token) 为键写会话 JSON，TTL = token_expire_seconds
        from llmpod.core.security import hash_token

        key, ttl, value = redis.setex_calls[0]
        assert key == f"{settings.token_key_prefix}{hash_token(result.token)}"
        assert ttl == settings.token_expire_seconds
        from llmpod.apps.webserver.auth.session import SessionData

        assert SessionData.model_validate_json(value) == sess
        # 登录更新 last_login_at 并 flush（MiniDB add/flush 不需要，行原地改）
        assert db.rows_of(User)[0].last_login_at is not None

    async def test_super_admin_role_flags_superuser(self, redis):
        db = _mkdb(tenant_id=0, roles=[_role(1, code="super_admin", tenant_id=0)])
        result = await authenticate(db, "alice", "pw", settings)
        assert result.session.is_superuser is True
        assert result.session.access_domain == "platform"

    async def test_platform_operator_domain_without_superuser(self, redis):
        db = _mkdb(tenant_id=0, roles=[_role(2, code="operator", tenant_id=0)])
        result = await authenticate(db, "alice", "pw", settings)
        assert result.session.access_domain == "platform"
        assert result.session.is_superuser is False

    async def test_custom_role_without_code_excluded_from_role_codes(self, redis):
        db = _mkdb(roles=[_role(12, code=None, tenant_id=5)])
        result = await authenticate(db, "alice", "pw", settings)
        assert result.session.role_ids == [12]
        assert result.session.role_codes == []

    async def test_wrong_password_invalid_credentials_with_subject(self, redis):
        from llmpod.core.security import hash_password

        db = _mkdb(password_hash=hash_password("right"), roles=[_role(11, code="tenant_admin", tenant_id=5)])
        with pytest.raises(AuthError) as ei:
            await authenticate(db, "alice", "wrong", settings)
        assert ei.value.error_code == ErrorCode.INVALID_CREDENTIALS
        assert ei.value.subject is db.rows_of(User)[0]  # 命中的注册用户供失败审计
        assert redis.setex_calls == []  # 不签发会话

    async def test_unknown_user_no_subject(self, redis):
        db = _mkdb(roles=[_role(11, code="tenant_admin", tenant_id=5)])
        with pytest.raises(AuthError) as ei:
            await authenticate(db, "nobody", "pw", settings)
        assert ei.value.error_code == ErrorCode.INVALID_CREDENTIALS
        assert ei.value.subject is None  # 防用户名枚举：不存在的用户不落 subject
        assert redis.setex_calls == []

    async def test_disabled_user_rejected(self, redis):
        db = _mkdb(status="disabled", roles=[_role(11, code="tenant_admin", tenant_id=5)])
        with pytest.raises(AuthError):
            await authenticate(db, "alice", "pw", settings)
        assert redis.setex_calls == []

    async def test_user_without_role_rejected(self, redis):
        db = _mkdb(roles=[])
        with pytest.raises(AuthError) as ei:
            await authenticate(db, "alice", "pw", settings)
        assert ei.value.error_code == ErrorCode.INVALID_CREDENTIALS
        assert redis.setex_calls == []


class TestLogout:
    async def test_removes_session_key(self, redis):
        from llmpod.core.security import hash_token

        await redis.setex(f"{settings.token_key_prefix}{hash_token('tok')}", 60, "{}")
        await logout("tok", settings)
        assert redis.deleted == [f"{settings.token_key_prefix}{hash_token('tok')}"]
        assert redis.strings == {}


class TestChangePassword:
    async def test_success_rotates_hash_and_commits(self, redis):
        from llmpod.core.security import hash_password, verify_password

        db = _mkdb(password_hash=hash_password("old-pass"))
        await change_password(db, user_id=100, old_password="old-pass", new_password="new-pass")
        row = db.rows_of(User)[0]
        assert verify_password("new-pass", row.password_hash)
        assert not verify_password("old-pass", row.password_hash)
        assert db.commits == 1

    async def test_wrong_old_password_rejected(self, redis):
        from llmpod.core.security import hash_password

        db = _mkdb(password_hash=hash_password("old-pass"))
        with pytest.raises(AuthError) as ei:
            await change_password(db, user_id=100, old_password="nope", new_password="x")
        assert ei.value.error_code == ErrorCode.INVALID_CREDENTIALS
        from llmpod.core.security import verify_password

        assert verify_password("old-pass", db.rows_of(User)[0].password_hash)  # 原哈希未动
        assert db.commits == 0

    async def test_unknown_user_rejected(self, redis):
        db = _mkdb()
        with pytest.raises(AuthError):
            await change_password(db, user_id=404, old_password="a", new_password="b")


class TestDetermineAccessDomain:
    def test_platform_when_any_platform_role(self):
        roles = [_role(1, code="tenant_admin", tenant_id=5), _role(2, code="operator", tenant_id=0)]
        assert _determine_access_domain(roles) == "platform"

    def test_tenant_when_only_tenant_roles(self):
        assert _determine_access_domain([_role(1, code="tenant_admin", tenant_id=5)]) == "tenant"
