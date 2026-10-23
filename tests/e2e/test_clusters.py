"""clusters 完整 CRUD 往返。"""

from __future__ import annotations

from httpx import Client

from _client import expect_error, ok, page


def test_cluster_round_trip(admin_client: Client, uniq):
    name = uniq("cluster")
    # create
    created = ok(admin_client, "POST", "/api/v1/admin/clusters", json={"name": name, "type": "docker"})
    cid = created["id"]
    assert created["name"] == name
    assert created["type"] == "docker"
    assert created["status"] == "active"
    # get
    got = ok(admin_client, "GET", f"/api/v1/admin/clusters/{cid}")
    assert got["name"] == name
    # list 包含
    listing = page(admin_client, "GET", "/api/v1/admin/clusters")
    assert any(c["id"] == cid for c in listing["items"])
    # update（改名 + 改状态）
    new_name = uniq("cluster-renamed")
    updated = ok(
        admin_client, "POST", f"/api/v1/admin/clusters/{cid}/update", json={"name": new_name, "status": "inactive"}
    )
    assert updated["name"] == new_name
    assert updated["status"] == "inactive"
    # delete
    deleted = ok(admin_client, "POST", f"/api/v1/admin/clusters/{cid}/delete")
    assert deleted == {"id": cid, "deleted": True}
    # 删后 get -> 404
    expect_error(admin_client, "GET", f"/api/v1/admin/clusters/{cid}", status=404)
