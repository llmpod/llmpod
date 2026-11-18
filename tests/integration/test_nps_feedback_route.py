"""NPS 反馈路由集成测试：路由注册 / 平台粗门 / Result 契约 / 校验 422。

HTTP 层只验依赖链与响应契约；submit_feedback 在路由模块命名空间打桩
（路由已 from ... import 绑定引用），不外呼收集端。
"""

from __future__ import annotations

import pytest

from llmpod.core.exceptions import LLMPodError

URL = "/api/v1/admin/nps"


@pytest.mark.asyncio
async def test_submit_ok(admin_client, monkeypatch):
    from llmpod.apps.webserver.routes.admin import nps as route

    calls = []

    async def fake_submit(db, request):
        calls.append(request)

    monkeypatch.setattr(route, "submit_feedback", fake_submit)
    resp = await admin_client.post(URL, json={"rate": 5, "comment": "还行还行"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["data"] is None
    # 前端弹窗产出的两字段原样进入服务层，其余字段不经过前端
    assert calls[0].rate == 5
    assert calls[0].comment == "还行还行"


@pytest.mark.asyncio
async def test_submit_upstream_failure_maps_400(admin_client, monkeypatch):
    from llmpod.apps.webserver.routes.admin import nps as route

    async def fake_submit(db, request):
        raise LLMPodError("反馈提交失败：错误的参数【SERVER_ID】")

    monkeypatch.setattr(route, "submit_feedback", fake_submit)
    resp = await admin_client.post(URL, json={"rate": 4})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 400
    assert body["error_code"] == "BUSINESS_ERROR"
    assert "错误的参数【SERVER_ID】" in body["message"]


@pytest.mark.asyncio
async def test_rate_out_of_range_rejected(admin_client):
    """收集端不校验 rate 量纲（实测 0/9 均 200），边界在本层收口。"""
    for bad in (0, 6, "5x"):
        resp = await admin_client.post(URL, json={"rate": bad})
        assert resp.status_code == 422, bad
        assert resp.json()["code"] == 422


@pytest.mark.asyncio
async def test_comment_length_capped(admin_client):
    resp = await admin_client.post(URL, json={"rate": 5, "comment": "长" * 501})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_requires_platform_login(client):
    """无登录态：平台粗门拒绝（401），不触达服务层。"""
    resp = await client.post(URL, json={"rate": 5})
    assert resp.status_code == 401
