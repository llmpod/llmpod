"""nodes 只读冒烟（无 basic create 端点；节点靠 manager WS 注册=complex）。"""

from __future__ import annotations

from httpx import Client

from _client import expect_error, ok, page


def test_nodes_list_smoke(admin_client: Client):
    # 既支持空测试栈，也支持已接入真实节点的实验栈。
    data = page(admin_client, "GET", "/api/v1/admin/nodes")
    assert data["total"] >= len(data["items"])
    for node in data["items"]:
        detail = ok(admin_client, "GET", f"/api/v1/admin/nodes/{node['id']}")
        assert detail["id"] == node["id"]
        assert detail["cluster_id"] == node["cluster_id"]


def test_node_get_not_found(admin_client: Client):
    expect_error(admin_client, "GET", "/api/v1/admin/nodes/nonexistent-node", status=404)
