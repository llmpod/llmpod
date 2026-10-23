"""E2E 测试 HTTP 客户端封装：同步 httpx + 统一 Result 解包 + 断言。

所有 webserver 响应遵循三段式 {code, message, data}（成功 code==200）；
错误带 error_code。本模块提供三个助手，保持测试体简洁、断言风格一致。
"""

from __future__ import annotations

import httpx


def ok(client: httpx.Client, method: str, path: str, **kwargs):
    """发请求并断言成功：HTTP 200 + body.code==200。返回 data。"""
    resp = client.request(method, path, **kwargs)
    assert resp.status_code == 200, f"{method} {path} -> {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("code") == 200, f"{method} {path} 非 200 契约: {body}"
    return body.get("data")


def page(client: httpx.Client, method: str, path: str, **kwargs):
    """同 ok，但额外断言 data 是分页结构（含 items/total/page/page_size）。返回 data。"""
    data = ok(client, method, path, **kwargs)
    assert isinstance(data, dict), f"{method} {path} 分页 data 不是 dict: {data}"
    for key in ("items", "total", "page", "page_size"):
        assert key in data, f"{method} {path} 分页缺字段 {key}: {data}"
    return data


def expect_error(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    status: int,
    error_code: str | None = None,
    **kwargs,
) -> dict:
    """断言错误响应：HTTP status（+ 可选 error_code）。返回整个 body。"""
    resp = client.request(method, path, **kwargs)
    assert resp.status_code == status, f"{method} {path} 期望 {status} 实得 {resp.status_code}: {resp.text}"
    body = resp.json()
    if error_code is not None:
        assert body.get("error_code") == error_code, f"{method} {path} 期望 error_code={error_code} 实得 {body}"
    return body
