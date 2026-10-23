"""租户端 access：本租户角色 CRUD + 角色权限授予/替换/回收 + 用户-角色追加/回收。

镜像 test_access.py（管理端平台域），全部走 tenant_client，固定本租户作用域。
"""

from __future__ import annotations

from httpx import Client

from _client import expect_error, ok


def _create_tenant_role(tenant_client: Client, uniq) -> dict:
    name = uniq("trole")
    return ok(
        tenant_client,
        "POST",
        "/api/v1/tenant/access/roles",
        json={"name": f"TRole {name}"},
    )


def test_tenant_role_crud_and_permissions(tenant_client: Client, uniq):
    # create（本租户自定义角色）
    created = _create_tenant_role(tenant_client, uniq)
    rid = created["id"]
    assert created["is_builtin"] is False
    # list（仅本租户）包含
    roles = ok(tenant_client, "GET", "/api/v1/tenant/access/roles")
    assert any(r["id"] == rid for r in roles)
    # 本租户内置角色也在列表里（seed 自动建）
    assert "tenant_operator" in {r["code"] for r in roles}
    # 权限初始为空
    assert ok(tenant_client, "GET", f"/api/v1/tenant/access/roles/{rid}/permissions") == []
    # 取两个租户域权限做授权
    perms = ok(
        tenant_client,
        "GET",
        "/api/v1/tenant/access/permissions",
    )
    assert len(perms) >= 2
    pids = [p["id"] for p in perms[:2]]
    # 全量替换
    replaced = ok(
        tenant_client,
        "POST",
        f"/api/v1/tenant/access/roles/{rid}/permissions",
        json={"permission_ids": pids},
    )
    assert replaced == {"role_id": rid, "permission_ids": sorted(pids)}
    assert len(ok(tenant_client, "GET", f"/api/v1/tenant/access/roles/{rid}/permissions")) == 2
    # 回收一个
    revoked = ok(
        tenant_client,
        "POST",
        f"/api/v1/tenant/access/roles/{rid}/permissions/{pids[0]}/revoke",
    )
    assert revoked == {"role_id": rid, "permission_id": pids[0], "revoked": True}
    assert len(ok(tenant_client, "GET", f"/api/v1/tenant/access/roles/{rid}/permissions")) == 1
    # update 改名
    updated = ok(
        tenant_client,
        "POST",
        f"/api/v1/tenant/access/roles/{rid}/update",
        json={"name": "TRenamed"},
    )
    assert updated["name"] == "TRenamed"
    # delete（非内置可删）
    deleted = ok(tenant_client, "POST", f"/api/v1/tenant/access/roles/{rid}/delete")
    assert deleted == {"id": rid, "deleted": True}
    # 删后查权限 404
    expect_error(tenant_client, "GET", f"/api/v1/tenant/access/roles/{rid}/permissions", status=404)


def test_tenant_role_builtin_undeletable(tenant_client: Client, test_tenant):
    roles = ok(tenant_client, "GET", "/api/v1/tenant/access/roles")
    builtin = next(r for r in roles if r["is_builtin"])
    # 内置角色不可删
    expect_error(
        tenant_client,
        "POST",
        f"/api/v1/tenant/access/roles/{builtin['id']}/delete",
        status=409,
        error_code="CONFLICT",
    )


def test_tenant_user_role_assign_revoke(tenant_client: Client, test_tenant, uniq):
    username = uniq("tu2")
    user = ok(
        tenant_client,
        "POST",
        "/api/v1/tenant/users",
        json={
            "username": username,
            "password": "Passw0rd!",
            "role_ids": [test_tenant["role_id"]],
        },
    )
    uid = user["id"]
    # 临时自定义角色（本租户）
    role = _create_tenant_role(tenant_client, uniq)
    rid = role["id"]
    # 追加角色
    added = ok(
        tenant_client,
        "POST",
        f"/api/v1/tenant/access/users/{uid}/roles",
        json={"role_ids": [rid]},
    )
    assert rid in added["added"]
    # 有效权限（list[str]）
    eff = ok(tenant_client, "GET", f"/api/v1/tenant/access/users/{uid}/permissions")
    assert isinstance(eff, list)
    # 回收自定义角色（非 tenant_admin，安全）
    revoked = ok(
        tenant_client,
        "POST",
        f"/api/v1/tenant/access/users/{uid}/roles/{rid}/revoke",
    )
    assert revoked == {"user_id": uid, "role_id": rid, "revoked": True}


def test_tenant_cannot_assign_cross_tenant_role(tenant_client: Client, admin_client: Client, test_tenant, uniq):
    """租户端给本租户用户分配平台域角色 → 拒绝（强制同租户绑定）。"""
    username = uniq("tu3")
    user = ok(
        tenant_client,
        "POST",
        "/api/v1/tenant/users",
        json={
            "username": username,
            "password": "Passw0rd!",
            "role_ids": [test_tenant["role_id"]],
        },
    )
    uid = user["id"]
    # 取一个平台域内置角色（管理端列表只返回平台域）
    platform_roles = ok(admin_client, "GET", "/api/v1/admin/access/roles")
    operator_role_id = next(r["id"] for r in platform_roles if r["code"] == "operator")
    # 跨租户分配应被拒（Conflict）
    expect_error(
        tenant_client,
        "POST",
        f"/api/v1/tenant/access/users/{uid}/roles",
        json={"role_ids": [operator_role_id]},
        status=409,
        error_code="CONFLICT",
    )


def test_login_user_with_codeless_custom_role(tenant_client: Client, base_url, test_tenant, uniq):
    """绑自定义角色（code=None）的用户登录不应 500：role_codes 过滤 None。"""
    username = uniq("tu4")
    password = "Passw0rd!"
    user = ok(
        tenant_client,
        "POST",
        "/api/v1/tenant/users",
        json={
            "username": username,
            "password": password,
            "role_ids": [test_tenant["role_id"]],
        },
    )
    uid = user["id"]
    # 追加一个无 code 的自定义角色（本租户）
    role = _create_tenant_role(tenant_client, uniq)
    ok(
        tenant_client,
        "POST",
        f"/api/v1/tenant/access/users/{uid}/roles",
        json={"role_ids": [role["id"]]},
    )
    # 该用户直接登录：内置 tenant_operator 有 code、自定义角色无 code，role_codes 不含 None
    import httpx as _httpx

    c = _httpx.Client(base_url=base_url, timeout=15.0)
    data = ok(c, "POST", "/api/v1/auth/login", json={"username": username, "password": password})
    assert data["username"] == username
    assert isinstance(data["role_codes"], list)
    assert None not in data["role_codes"]
    c.close()
