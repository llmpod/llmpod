"""测试 fixtures。

集成测试遵循"stub get_db + 鉴权覆盖"原则，不依赖外部 PostgreSQL / Redis：
get_db 被覆盖为桩会话，真正需要落库的 service 由具体测试自行 monkeypatch。
HTTP 层只验证路由注册、依赖链、统一 Result 响应契约与异常→HTTP 映射。
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient


class _StubSession:
    """桩会话：断开真实数据库。

    仅实现写侧 async 方法（commit/rollback/flush/refresh）为 no-op——写端点现在
    在返回前显式 ``await db.commit()``，桩须能承接。读侧查询（execute/add/delete/get）
    不在此实现：需要查询的端点由各测试 monkeypatch 对应 service 函数（见 testing 规则）。
    ``get`` 返回 None（NotFound→404），供校验「请求 schema 合法 + 路由触达」的契约测试用。
    """

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def flush(self):
        pass

    async def refresh(self, *_args, **_kwargs):
        pass

    async def get(self, *_args, **_kwargs):
        return None


async def _stub_get_db():
    """桩数据库会话：永不连接真实数据库。

    yield 一个写侧 no-op 的 _StubSession；凡 Depends(get_db) 的端点若真正需要查询，
    须由该测试 monkeypatch 对应的 service 函数，而不是依赖此会话。
    """
    yield _StubSession()


@pytest_asyncio.fixture
async def client():
    """HTTP 测试客户端：覆盖 get_db，断开真实数据库连接。

    仅覆盖 get_db（让 require_permission 等依赖解析 get_db 时不触库），不覆盖鉴权
    ——需要登录态/超管的用例在测试体内对 ``app.dependency_overrides`` 追加
    get_current_user 等覆盖；本 fixture 的 teardown 会统一清理。
    """
    from llmpod.app import app
    from llmpod.db import get_db

    app.dependency_overrides[get_db] = _stub_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client(client):
    """client + 超管会话：需要平台登录态的 admin 路由用例直接用（teardown 统一清理）。

    会话形状经 ``_fakes.admin_session``（is_superuser=True 短路权限校验，不触库）。
    """
    from llmpod.app import app
    from llmpod.apps.webserver.auth.deps import get_current_user
    from tests.integration._fakes import admin_session

    app.dependency_overrides[get_current_user] = lambda: admin_session()
    return client
