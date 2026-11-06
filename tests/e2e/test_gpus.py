"""gpus 只读冒烟（GPU 为节点上报派生资源，只读）。"""

from __future__ import annotations

from httpx import Client

from _client import expect_error, ok, page


def test_gpus_list_smoke(admin_client: Client):
    data = page(admin_client, "GET", "/api/v1/admin/gpus")
    assert data["total"] >= len(data["items"])
    for gpu in data["items"]:
        detail = ok(admin_client, "GET", f"/api/v1/admin/gpus/{gpu['id']}")
        assert detail["uuid"] == gpu["uuid"]
        assert detail["node_id"] == gpu["node_id"]


def test_gpu_get_not_found(admin_client: Client):
    expect_error(admin_client, "GET", "/api/v1/admin/gpus/999999", status=404)
