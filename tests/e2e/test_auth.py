"""认证端点 E2E：login / me / menus / logout + 代表性 401。"""

from __future__ import annotations

from httpx import Client

from _client import expect_error, ok


def test_login_admin(client: Client):
    data = ok(client, "POST", "/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert data["username"] == "admin"
    assert data["is_superuser"] is True
    assert data["access_domain"] == "platform"
    assert isinstance(data["token"], str) and data["token"]
    # LoginData 契约：不含 tenant_id / role_ids
    assert "tenant_id" not in data
    assert "role_ids" not in data


def test_login_bad_credentials(client: Client):
    expect_error(
        client,
        "POST",
        "/api/v1/auth/login",
        status=401,
        error_code="INVALID_CREDENTIALS",
        json={"username": "admin", "password": "wrong"},
    )


def test_protected_endpoint_requires_token(client: Client):
    # 无 token 访问受保护端点 -> 401（证明真实鉴权中间件在栈内生效）
    expect_error(client, "GET", "/api/v1/auth/me", status=401)


def test_me_and_menus(admin_client: Client):
    me = ok(admin_client, "GET", "/api/v1/auth/me")
    assert me["username"] == "admin"
    assert me["is_superuser"] is True
    assert me["tenant_id"] == 0
    assert me["access_domain"] == "platform"
    menus = ok(admin_client, "GET", "/api/v1/auth/menus")
    # 契约：key=前端路由路径，value 仅 enabled（平台开关标注）
    assert isinstance(menus, dict)
    assert "/admin/dashboard" in menus
    assert set(menus["/admin/dashboard"]) == {"enabled"}
    assert menus["/admin/dashboard"]["enabled"] is True


def test_logout_invalidates_token(client: Client):
    # 用独立登录的 token 验证登出（不触碰 admin_client 的 session token）
    data = ok(client, "POST", "/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    token = data["token"]
    headers = {"Authorization": f"Bearer {token}"}
    ok(client, "POST", "/api/v1/auth/logout", headers=headers)
    resp = client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 401
