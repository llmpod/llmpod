"""access：roles CRUD + 角色权限授予/替换/回收 + 用户-角色追加/回收。"""

from __future__ import annotations

from httpx import Client

from _client import ok


def _create_role(admin_client: Client, uniq) -> dict:
    name = uniq("role")
    return ok(
        admin_client,
        "POST",
        "/api/v1/admin/access/roles",
        json={
            "name": f"Role {name}",
        },
    )


def test_role_crud_and_permissions(admin_client: Client, uniq):
    # create（平台域自定义角色）
    created = _create_role(admin_client, uniq)
    rid = created["id"]
    assert created["is_builtin"] is False
    # list 包含
    roles = ok(admin_client, "GET", "/api/v1/admin/access/roles")
    assert any(r["id"] == rid for r in roles)
    # 权限初始为空
    assert ok(admin_client, "GET", f"/api/v1/admin/access/roles/{rid}/permissions") == []
    # 取两个平台权限做授权
    perms = ok(admin_client, "GET", "/api/v1/admin/access/permissions", params={"scope": "platform"})
    assert len(perms) >= 2
    pids = [p["id"] for p in perms[:2]]
    # 全量替换
    replaced = ok(admin_client, "POST", f"/api/v1/admin/access/roles/{rid}/permissions", json={"permission_ids": pids})
    assert replaced == {"role_id": rid, "permission_ids": sorted(pids)}
    assert len(ok(admin_client, "GET", f"/api/v1/admin/access/roles/{rid}/permissions")) == 2
    # 回收一个
    revoked = ok(admin_client, "POST", f"/api/v1/admin/access/roles/{rid}/permissions/{pids[0]}/revoke")
    assert revoked == {"role_id": rid, "permission_id": pids[0], "revoked": True}
    assert len(ok(admin_client, "GET", f"/api/v1/admin/access/roles/{rid}/permissions")) == 1
    # update 改名
    updated = ok(admin_client, "POST", f"/api/v1/admin/access/roles/{rid}/update", json={"name": "Renamed"})
    assert updated["name"] == "Renamed"
    # delete（非内置可删）
    deleted = ok(admin_client, "POST", f"/api/v1/admin/access/roles/{rid}/delete")
    assert deleted == {"id": rid, "deleted": True}


def test_user_role_assign_revoke(admin_client: Client, uniq):
    # 平台域临时用户（管理员端只管 tenant_id=0），取内置 operator 角色
    roles = ok(admin_client, "GET", "/api/v1/admin/access/roles")
    by_code = {r["code"]: r["id"] for r in roles}
    operator_role_id = by_code["operator"]
    username = uniq("u2")
    user = ok(
        admin_client,
        "POST",
        "/api/v1/admin/users",
        json={
            "username": username,
            "password": "Passw0rd!",
            "role_ids": [operator_role_id],
        },
    )
    uid = user["id"]
    # 临时自定义角色（平台域）
    role = _create_role(admin_client, uniq)
    rid = role["id"]
    # 追加角色
    added = ok(admin_client, "POST", f"/api/v1/admin/access/users/{uid}/roles", json={"role_ids": [rid]})
    assert rid in added["added"]
    # 有效权限（list[str]）
    eff = ok(admin_client, "GET", f"/api/v1/admin/access/users/{uid}/permissions")
    assert isinstance(eff, list)
    # 回收角色（非 ADMIN_ROLE_CODES，安全）
    revoked = ok(admin_client, "POST", f"/api/v1/admin/access/users/{uid}/roles/{rid}/revoke")
    assert revoked == {"user_id": uid, "role_id": rid, "revoked": True}
