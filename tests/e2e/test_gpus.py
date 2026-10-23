"""gpus 只读冒烟（GPU 为节点上报派生资源，只读）。"""

from __future__ import annotations

from httpx import Client

from _client import expect_error, page


def test_gpus_list_smoke(admin_client: Client):
    data = page(admin_client, "GET", "/api/v1/admin/gpus")
    assert data["items"] == []
    assert data["total"] == 0


def test_gpu_get_not_found(admin_client: Client):
    expect_error(admin_client, "GET", "/api/v1/admin/gpus/999999", status=404)
