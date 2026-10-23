"""tenants 完整 CRUD 往返（用一次性租户，不碰平台租户 id=0）。"""

from __future__ import annotations

import httpx

from _client import expect_error, ok, page


def test_tenant_round_trip(admin_client: httpx.Client, base_url: str, uniq):
    name = uniq("tenant-rt")
    admin_username = uniq("rtadmin")
    admin_password = "RtAdmin0rd!"
    # create（含引导租户超管：传用户名/密码，角色写死 tenant_admin）
    created = ok(
        admin_client,
        "POST",
        "/api/v1/admin/tenants",
        json={
            "name": name,
            "admin_username": admin_username,
            "admin_password": admin_password,
        },
    )
    tid = created["id"]
    assert created["name"] == name
    assert created["status"] == "active"
    assert created["user_count"] >= 1  # 引导超管已建
    # 引导超管可登录且为租户域、非超管
    c = httpx.Client(base_url=base_url, timeout=15.0)
    login = ok(c, "POST", "/api/v1/auth/login", json={"username": admin_username, "password": admin_password})
    assert login["access_domain"] == "tenant"
    assert login["is_superuser"] is False
    c.close()
    # get
    got = ok(admin_client, "GET", f"/api/v1/admin/tenants/{tid}")
    assert got["name"] == name
    # list 包含
    listing = page(admin_client, "GET", "/api/v1/admin/tenants")
    assert any(t["id"] == tid for t in listing["items"])
    # update（改名）
    new_name = uniq("tenant-renamed")
    updated = ok(admin_client, "POST", f"/api/v1/admin/tenants/{tid}/update", json={"name": new_name})
    assert updated["name"] == new_name
    # delete（级联清空引导超管及其角色，可删）
    deleted = ok(admin_client, "POST", f"/api/v1/admin/tenants/{tid}/delete")
    assert deleted == {"id": tid, "deleted": True}
    expect_error(admin_client, "GET", f"/api/v1/admin/tenants/{tid}", status=404)
