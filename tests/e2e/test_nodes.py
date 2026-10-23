"""nodes 只读冒烟（无 basic create 端点；节点靠 manager WS 注册=complex）。"""

from __future__ import annotations

from httpx import Client

from _client import expect_error, page


def test_nodes_list_smoke(admin_client: Client):
    # 测试栈无 node agent 注册，列表应为空但分页结构正确
    data = page(admin_client, "GET", "/api/v1/admin/nodes")
    assert data["items"] == []
    assert data["total"] == 0


def test_node_get_not_found(admin_client: Client):
    expect_error(admin_client, "GET", "/api/v1/admin/nodes/nonexistent-node", status=404)
