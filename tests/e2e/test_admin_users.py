"""admin users 完整 CRUD 往返（管理员端只管 tenant_id=0 平台用户）。"""

from __future__ import annotations

import httpx

from _client import expect_error, ok, page


def test_admin_user_round_trip(admin_client: httpx.Client, uniq):
    # 取一个平台域角色（tenant_id=0 的内置 operator，非 ADMIN_ROLE_CODES，删用户不被末位守卫拦）
    roles = ok(admin_client, "GET", "/api/v1/admin/access/roles")
    by_code = {r["code"]: r["id"] for r in roles}
    operator_role_id = by_code["operator"]
    username = uniq("user")
    # create（管理员端：不传 tenant_id，固定平台域 tenant_id=0）
    created = ok(
        admin_client,
        "POST",
        "/api/v1/admin/users",
        json={
            "username": username,
            "password": "Passw0rd!",
            "role_ids": [operator_role_id],
        },
    )
    uid = created["id"]
    assert created["username"] == username
    assert created["tenant_id"] == 0
    assert created["status"] == "active"
    assert "password_hash" not in created  # 密码哈希不得外泄
    # 创建响应即带回 role_ids（富化装配），前端无需二次查即可回显角色
    assert created["role_ids"] == [operator_role_id]
    # get
    got = ok(admin_client, "GET", f"/api/v1/admin/users/{uid}")
    assert got["username"] == username
    assert got["role_ids"] == [operator_role_id]
    # list（管理员端只查平台域 tenant_id=0，包含新建；无 tenant_id 过滤参数）
    listing = page(admin_client, "GET", "/api/v1/admin/users")
    assert any(u["id"] == uid for u in listing["items"])
    # 列表项也带 role_ids（批量装配）
    assert next(u["role_ids"] for u in listing["items"] if u["id"] == uid) == [operator_role_id]
    # update（改邮箱 + 禁用）
    updated = ok(
        admin_client, "POST", f"/api/v1/admin/users/{uid}/update", json={"email": "u@e2e.test", "status": "disabled"}
    )
    assert updated["email"] == "u@e2e.test"
    assert updated["status"] == "disabled"
    # delete（operator 非末位管理员角色，可删）
    deleted = ok(admin_client, "POST", f"/api/v1/admin/users/{uid}/delete")
    assert deleted == {"id": uid, "deleted": True}
    expect_error(admin_client, "GET", f"/api/v1/admin/users/{uid}", status=404)
