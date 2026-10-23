"""健康检查端点冒烟。"""

from __future__ import annotations

from httpx import Client


def test_health_ok(client: Client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
