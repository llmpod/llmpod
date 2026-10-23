"""租户域：profile + tenant users 完整往返。"""

from __future__ import annotations

from httpx import Client

from _client import ok


def test_tenant_profile(tenant_client: Client, test_tenant):
    prof = ok(tenant_client, "GET", "/api/v1/tenant/profile")
    assert prof["tenant"]["id"] == test_tenant["tenant_id"]
    assert prof["tenant"]["status"] == "active"
    assert isinstance(prof["user_count"], int)


def test_tenant_user_round_trip(tenant_client: Client, test_tenant, uniq):
    username = uniq("tu")
    # create（无 tenant_id，取自会话）
    created = ok(
        tenant_client,
        "POST",
        "/api/v1/tenant/users",
        json={
            "username": username,
            "password": "Passw0rd!",
            "role_ids": [test_tenant["role_id"]],
        },
    )
    uid = created["id"]
    assert created["username"] == username
    assert created["tenant_id"] == test_tenant["tenant_id"]
    assert "password_hash" not in created
    # get
    ok(tenant_client, "GET", f"/api/v1/tenant/users/{uid}")
    # list（非分页）包含
    users = ok(tenant_client, "GET", "/api/v1/tenant/users")
    assert isinstance(users, list)
    assert any(u["id"] == uid for u in users)
    # update
    updated = ok(tenant_client, "POST", f"/api/v1/tenant/users/{uid}/update", json={"email": "tu@e2e.test"})
    assert updated["email"] == "tu@e2e.test"
    # delete
    deleted = ok(tenant_client, "POST", f"/api/v1/tenant/users/{uid}/delete")
    assert deleted == {"id": uid, "deleted": True}
