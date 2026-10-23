"""端点探测用例共用的桩（单测与集成两份用例的桩逐字相同，故共用一处）。

沿用 ``tests/integration/_op_session.py`` 的思路：本目录级共用件不改既有文件，只服务
新增用例，省掉同一段桩抄两遍后各自漂移。

- ``install_probe_transport``：把探测模块内自建的 httpx.AsyncClient 换成挂 MockTransport
  的桩，并返回请求记录供断言；只替换探测模块看到的 ``httpx`` 名字，**不**全局改
  ``httpx.AsyncClient``（那会把同测试内其它地方，含 ASGI 测试客户端，一起换掉）；
- ``replies`` / ``unreachable``：最常用的两个上游桩（按状态码应答 / 连接层失败）；
- ``endpoint_row``：已登记的外部端点行（默认 OpenAI + 带 Key + 未登记主路径 + 健康）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from llmpod.apps.proxyserver.snapshot import RouteTarget
from llmpod.apps.webserver.services import endpoint_probe
from llmpod.apps.webserver.services.endpoint_probe import ProbeTarget
from llmpod.models import ModelEndpoint


def install_probe_transport(monkeypatch, handler) -> list[httpx.Request]:
    """把探测模块内自建的 httpx.AsyncClient 换成挂 MockTransport 的桩，返回请求记录。"""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_cls = httpx.AsyncClient

    def _client(*_args, **_kwargs):
        return real_cls(transport=httpx.MockTransport(_record))

    monkeypatch.setattr(
        endpoint_probe,
        "httpx",
        SimpleNamespace(
            AsyncClient=_client,
            HTTPError=httpx.HTTPError,
            InvalidURL=httpx.InvalidURL,
            TimeoutException=httpx.TimeoutException,
        ),
    )
    return seen


def replies(status: int, body: str = '{"ok":true}'):
    """上游按固定状态码与响应体应答（2xx 缺省给合法 JSON——判据要求 2xx 且可解析）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode(), headers={"content-type": "application/json"})

    return handler


def unreachable(request: httpx.Request) -> httpx.Response:
    """上游连不上（DNS/拒连）：探测应报连接失败，不带状态码。"""
    raise httpx.ConnectError("Name or service not known")


def endpoint_row(**overrides) -> ModelEndpoint:
    """已登记的外部端点行。

    时间戳是必需的：真实 flush 由 ``server_default`` 回填，桩会话里缺了它们
    ``ModelEndpointOut``（非空列在 DTO 里必填）序列化会失败。
    """
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    base = dict(
        id=7,
        deployment_id=None,
        engine_type="external",
        protocol="openai",
        api_base="https://api.example.com/v1",
        path=None,
        api_key="sk-stored",
        served_model="gpt-4o",
        name="示例端点",
        operations=[],
        capabilities=[],
        enabled=True,
        health_status="healthy",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return ModelEndpoint(**base)


def probe_target(**overrides) -> ProbeTarget:
    """探测输入（关键字工厂：五个字段按位置写极易错位，且各用例都只关心其中一两个）。"""
    base = dict(
        protocol="openai", api_base="https://api.example.com/v1", path=None, served_model="gpt-4o", api_key="sk-1"
    )
    base.update(overrides)
    return ProbeTarget(**base)


def parity_route_target(probe: ProbeTarget, **overrides) -> RouteTarget:
    """按同一份探测输入造 API 网关的 RouteTarget（一致性守卫用；与 tests/unit/_fakes.py
    的 route_target 同名不同形，故改名避免互相 import 错）。

    URL 相关字段照抄探测输入，其余（weight / priority / engine_type / deployment_id）填中性
    值——守卫比对的是地址与头，不是选路。
    """
    base = dict(
        endpoint_id=1,
        deployment_id=None,
        api_base=probe.api_base,
        path=probe.path,
        served_model=probe.served_model,
        engine_type="external",
        weight=1,
        priority=1,
        protocol=probe.protocol,
        api_key=probe.api_key,
    )
    base.update(overrides)
    return RouteTarget(**base)
