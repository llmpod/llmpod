"""system config: list / get / update-or-create。非分页；无 delete。"""

from __future__ import annotations

from httpx import Client

from _client import expect_error, ok


def test_system_config_round_trip(admin_client: Client, uniq):
    key = "e2e." + uniq("cfg")  # 唯一 key 即可
    # 初始不存在 -> 404
    expect_error(admin_client, "GET", f"/api/v1/admin/system/config/{key}", status=404)
    # upsert 创建
    res = ok(
        admin_client,
        "POST",
        f"/api/v1/admin/system/config/{key}/update",
        json={"value": {"k": 1}, "description": "e2e"},
    )
    assert res == {"key": key, "updated": True}
    # get
    got = ok(admin_client, "GET", f"/api/v1/admin/system/config/{key}")
    assert got["key"] == key
    assert got["value"] == {"k": 1}
    # list（非分页）包含
    listing = ok(admin_client, "GET", "/api/v1/admin/system/config")
    assert isinstance(listing, list)
    assert any(c["key"] == key for c in listing)
    # update value
    ok(admin_client, "POST", f"/api/v1/admin/system/config/{key}/update", json={"value": {"k": 2}})
    got2 = ok(admin_client, "GET", f"/api/v1/admin/system/config/{key}")
    assert got2["value"] == {"k": 2}
