"""E2E 测试公共 fixtures。

隔离闸：未设 LLMPOD_E2E=1 时整 session skip，保证 `make test`（跑 tests/）下
e2e 不会因为没有运行中的测试栈而失败。仅 `run.sh`（make test-e2e）会设置该变量。

fixtures：base_url（来自 run.sh 注入）、client（无鉴权 httpx.Client，供 401 冒烟）。
后续 task 在此追加 uniq、admin_client、test_tenant、tenant_client。
"""

from __future__ import annotations

import os

import httpx
import pytest
from _client import ok
import uuid


@pytest.fixture(scope="session", autouse=True)
def _e2e_gate():
    """LLMPOD_E2E!=1 时跳过整个 e2e session。"""
    if os.environ.get("LLMPOD_E2E") != "1":
        pytest.skip("e2e 需运行中的测试栈；用 `make test-e2e` 启动")


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ["LLMPOD_E2E_BASE_URL"]


@pytest.fixture(scope="session")
def client(base_url) -> httpx.Client:
    """无鉴权客户端（供代表性 401 冒烟）。"""
    c = httpx.Client(base_url=base_url, timeout=15.0)
    yield c
    c.close()


@pytest.fixture(scope="session")
def uniq():
    """生成 session 内唯一资源名：uniq("cluster") -> "cluster-<tag>-<n>"。"""
    tag = uuid.uuid4().hex[:8]
    counter = {"n": 0}

    def _make(base: str) -> str:
        counter["n"] += 1
        return f"{base}-{tag}-{counter['n']}"

    return _make


@pytest.fixture(scope="session")
def admin_client(base_url) -> httpx.Client:
    """超管登录态客户端：admin/admin 登录，注入 Bearer token。session 级，跨测试复用。"""
    c = httpx.Client(base_url=base_url, timeout=15.0)
    data = ok(c, "POST", "/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert data["username"] == "admin"
    assert data["is_superuser"] is True
    c.headers["Authorization"] = f"Bearer {data['token']}"
    yield c
    c.close()


@pytest.fixture(scope="session")
def test_tenant(admin_client, uniq):
    """会话级测试租户：建租户(自动 seed 内置角色 + 引导超管) -> 返回
    {tenant_id, role_id, admin_role_id, name, admin_username, admin_password}。

    - role_id：tenant_operator 角色 id（非 ADMIN_ROLE_CODES，删除其用户不会被末位管理员守卫拦截）。
    - admin_role_id：tenant_admin 角色 id（具 tenant:user:create/update/delete 全套权限）。
    - admin_username/admin_password：创建租户时引导的租户超管凭据（用于 tenant_client 登录）。
    """
    name = uniq("tenant")
    admin_username = uniq("tadmin")
    admin_password = "TenantAdmin0rd!"
    t = ok(
        admin_client,
        "POST",
        "/api/v1/admin/tenants",
        json={
            "name": name,
            "admin_username": admin_username,
            "admin_password": admin_password,
        },
    )
    login = ok(
        admin_client,
        "POST",
        "/api/v1/auth/login",
        json={"username": admin_username, "password": admin_password},
    )
    tenant_scoped = httpx.Client(base_url=admin_client.base_url, timeout=15.0)
    tenant_scoped.headers["Authorization"] = f"Bearer {login['token']}"
    roles = ok(tenant_scoped, "GET", "/api/v1/tenant/access/roles")
    tenant_scoped.close()
    by_code = {r["code"]: r["id"] for r in roles}
    return {
        "tenant_id": t["id"],
        "role_id": by_code["tenant_operator"],
        "admin_role_id": by_code["tenant_admin"],
        "name": name,
        "admin_username": admin_username,
        "admin_password": admin_password,
    }


@pytest.fixture(scope="session")
def tenant_client(base_url, test_tenant) -> httpx.Client:
    """租户域登录态客户端：以创建租户时引导的租户超管（tenant_admin）登录，
    具 tenant:user:* 全套权限，供租户用户 CRUD。"""
    c = httpx.Client(base_url=base_url, timeout=15.0)
    data = ok(
        c,
        "POST",
        "/api/v1/auth/login",
        json={
            "username": test_tenant["admin_username"],
            "password": test_tenant["admin_password"],
        },
    )
    assert data["access_domain"] == "tenant"
    assert data["is_superuser"] is False
    c.headers["Authorization"] = f"Bearer {data['token']}"
    yield c
    c.close()
