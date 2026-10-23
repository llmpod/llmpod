"""首次引导弹窗（first_guide）测试：状态机、降级、登录触发门控、管理端消耗。

语义：系统首次平台超管成功登录 → 触发弹窗（first_guide=true 随种子注入
管理端，登录落地弹一键部署向导）；首个携带待弹出态的管理端 index.html 服务
→ 消耗弹出机会、永久不再弹。状态机走 FakeSession 验证；路由/注入侧只验
调用门控与「读到待弹出态才消耗」。
"""

from __future__ import annotations

from httpx import AsyncClient

from _fakes import FakeSession, admin_session

from llmpod.apps.webserver.auth.session import LoginResult
from llmpod.core.exceptions import AuthError, ErrorCode
from llmpod.models.system_config import SystemConfig
from llmpod.services.first_guide_service import (
    FIRST_GUIDE_KEY,
    consume_first_guide,
    first_guide_open,
    open_first_guide,
)


def _guide_row(db: FakeSession) -> SystemConfig | None:
    return next((r for r in db.all(SystemConfig) if r.key == FIRST_GUIDE_KEY), None)


class _BrokenExecuteSession(FakeSession):
    """execute 必炸：读侧降级（按不弹返回、不抛）。"""

    async def execute(self, *_a, **_k):
        raise RuntimeError("db down")


class _FlushBoomSession(FakeSession):
    """flush 必炸：写侧落库异常在 SAVEPOINT 内吞掉。

    断言点：不抛（调用方照常）、不触碰调用方事务边界（rollback/commit 都
    不许调——rollback 会把 authenticate 已 flush 的 last_login_at 卷走）。
    """

    def __init__(self):
        super().__init__()
        self.rolled_back = False
        self.committed = False

    async def flush(self):
        raise RuntimeError("flush boom")

    async def rollback(self):
        self.rolled_back = True

    async def commit(self):
        self.committed = True


class TestGuideLifecycle:
    async def test_fresh_system_closed_and_consume_noop(self):
        """全新系统（无标记行）：读作不弹、消耗 no-op——不产生幽灵行。"""
        db = FakeSession()
        assert await first_guide_open(db) is False
        await consume_first_guide(db)
        assert _guide_row(db) is None

    async def test_first_login_opens_window(self):
        """首次登录：置 open 行（弹窗待弹出，updated_by 行元数据记首登者）。"""
        db = FakeSession()
        await open_first_guide(db, user_id=1)
        row = _guide_row(db)
        assert row is not None
        assert row.value == {"state": "open"}
        assert row.updated_by == 1
        assert await first_guide_open(db) is True

    async def test_open_is_idempotent(self):
        """已触发再触发：幂等不动——登录侧只写一次，终态不会被重新拉起。"""
        db = FakeSession()
        await open_first_guide(db, user_id=1)
        await open_first_guide(db, user_id=2)
        row = _guide_row(db)
        assert row.value == {"state": "open"}
        assert row.updated_by == 1  # 首登者不被覆盖

    async def test_consume_closes_permanently(self):
        """消耗：open → consumed 弹过即永久关闭；再消耗幂等、触发不再有效。"""
        db = FakeSession()
        await open_first_guide(db, user_id=1)
        await consume_first_guide(db)
        assert _guide_row(db).value == {"state": "consumed"}
        assert await first_guide_open(db) is False
        await consume_first_guide(db)  # 幂等
        await open_first_guide(db, user_id=2)  # 终态不可重开
        assert _guide_row(db).value == {"state": "consumed"}

    async def test_invalid_value_reads_closed_and_stays_untouched(self):
        """非法 value（通用 config 接口可写任意结构 / 手工 SQL 产物）：读作
        不弹、触发/消耗都不碰行——既不能重新拉起弹窗，也不能预写压制首登引导。"""
        for value in ({}, "broken", {"state": "weird"}):
            db = FakeSession()
            db.preload(SystemConfig(key=FIRST_GUIDE_KEY, value=value, description=None))
            assert await first_guide_open(db) is False
            await open_first_guide(db, user_id=1)
            await consume_first_guide(db)
            assert _guide_row(db).value == value

    async def test_read_failure_degrades_closed(self):
        """读失败按不弹：引导故障不连累设置种子注入（页面照常）。"""
        assert await first_guide_open(_BrokenExecuteSession()) is False

    async def test_write_failure_isolated_to_savepoint(self):
        """落库异常只回滚 SAVEPOINT：不抛、不 rollback/commit 调用方 session。"""
        session = _FlushBoomSession()
        await open_first_guide(session, user_id=1)
        await consume_first_guide(session)
        assert session.rolled_back is False
        assert session.committed is False


class TestLoginOpensWindow:
    """登录路由挂钩：仅平台超管的成功登录触发弹窗；其余登录/失败不触发。"""

    async def _login(self, client: AsyncClient, monkeypatch, session):
        async def fake_authenticate(db, username, password, settings):
            return LoginResult(token="stub-token", session=session)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.authenticate", fake_authenticate)
        return await client.post("/api/v1/auth/login", json={"username": "u", "password": "pw"})

    def _track_open(self, monkeypatch) -> list:
        calls = []

        async def fake_open(db, *, user_id):
            calls.append(user_id)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.open_first_guide", fake_open)
        return calls

    async def test_superuser_login_opens_guide(self, client: AsyncClient, monkeypatch):
        calls = self._track_open(monkeypatch)
        response = await self._login(client, monkeypatch, admin_session())
        assert response.status_code == 200
        assert calls == [1]

    async def test_non_superuser_login_skips_guide(self, client: AsyncClient, monkeypatch):
        """非超管登录不触发：租户/平台 operator 的登录不参与首登判定。"""
        calls = self._track_open(monkeypatch)
        response = await self._login(client, monkeypatch, admin_session(is_superuser=False))
        assert response.status_code == 200
        assert calls == []

    async def test_credentials_failure_skips_guide(self, client: AsyncClient, monkeypatch):
        """凭据失败在触发之前抛出：不触发（失败尝试不算登录）。"""
        calls = self._track_open(monkeypatch)

        async def fail_authenticate(db, username, password, settings):
            raise AuthError("用户名或密码错误", error_code=ErrorCode.INVALID_CREDENTIALS)

        monkeypatch.setattr("llmpod.apps.webserver.routes.public.auth.authenticate", fail_authenticate)
        response = await client.post("/api/v1/auth/login", json={"username": "u", "password": "bad"})
        assert response.status_code == 401
        assert calls == []
