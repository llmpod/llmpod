"""会话版本以用户表为权威：停用、删除、降权及旧版本均即时失效。"""

from __future__ import annotations

import pytest

from llmpod.apps.webserver.auth.session import SessionData
from llmpod.core.exceptions import AuthError
from llmpod.db import redis as db_redis
from llmpod.models.user import User
from llmpod.services.session_version_service import bump_version
from tests.unit._minidb import MiniDB


class MemoryRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)


@pytest.fixture
def auth_env(monkeypatch):
    from llmpod.apps.webserver.auth import deps

    redis = MemoryRedis()
    db = MiniDB()
    user = db.preload(User(id=100, tenant_id=5, username="alice", password_hash="x", status="active", auth_version=1))
    monkeypatch.setattr(db_redis, "_redis_client", redis)
    monkeypatch.setattr(deps, "_extract_token", lambda _req: "raw-token")
    monkeypatch.setattr(deps, "hash_token", lambda _token: "abc")
    monkeypatch.setattr(deps, "get_settings", lambda: type("S", (), {"token_key_prefix": "llmpod:token:"})())

    class Request:
        headers = {}
        client = None

    def issue(version=1):
        session = SessionData(
            user_id=100, tenant_id=5, access_domain="platform", is_superuser=True, session_ver=version
        )
        redis.store["llmpod:token:abc"] = session.model_dump_json()

    return db, user, Request(), issue


async def test_valid_session_passes(auth_env):
    from llmpod.apps.webserver.auth.deps import get_current_user

    db, _user, request, issue = auth_env
    issue()
    assert (await get_current_user(request, None, db)).user_id == 100


@pytest.mark.parametrize("change", ["disable", "delete", "bump"])
async def test_old_session_rejected_after_auth_change(auth_env, change):
    from llmpod.apps.webserver.auth.deps import get_current_user

    db, user, request, issue = auth_env
    issue()
    if change == "disable":
        user.status = "disabled"
    elif change == "delete":
        await db.delete(user)
    else:
        await bump_version(user.id, db)
    await db.commit()
    # 消融：若仅信任 Redis 会话快照，token 仍存在且超管标志仍为 True，
    # 停用/删除/降权后会继续短路权限检查；DB 权威检查使同一 token 失效。
    snapshot = SessionData.model_validate_json(db_redis._redis_client.store["llmpod:token:abc"])
    assert snapshot.is_superuser is True
    with pytest.raises(AuthError):
        await get_current_user(request, None, db)


async def test_legacy_zero_version_session_rejected(auth_env):
    from llmpod.apps.webserver.auth.deps import get_current_user

    db, _user, request, issue = auth_env
    issue(version=0)
    with pytest.raises(AuthError):
        await get_current_user(request, None, db)


async def test_bump_refreshes_identity_map_after_row_lock():
    from types import SimpleNamespace

    cached = User(id=100, tenant_id=5, username="alice", password_hash="x", status="active", auth_version=1)

    class RacingSession:
        async def execute(self, stmt):
            # 消融：无 populate_existing 时 SQLAlchemy 会返还 auth_version=1
            # 的缓存对象，尽管等待行锁后数据库值已经是 2。
            if stmt.get_execution_options().get("populate_existing"):
                cached.auth_version = 2
            return SimpleNamespace(scalar_one_or_none=lambda: cached)

    await bump_version(100, RacingSession())
    assert cached.auth_version == 3
