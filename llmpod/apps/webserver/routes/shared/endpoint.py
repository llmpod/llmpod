"""网关接入地址（Proxyserver 端点信息，跨域公共只读）。

给前端展示给用户「API 该打到哪」：优先显式配置的宣告地址（固定 IP/域名、Nginx 统一
接入的 external_url 场景）；否则按请求方访问 Manager 用的 Host 推导
（``{scheme}://{Host 主机}:{proxy_port}`` 即「你能这样访问到我，就大概率能这样访问
proxyserver」，scheme 随请求方 http/https），内网 LAN IP、公网 IP/域名、同机 localhost 天然各自正确；
仅当无可用 Host 时才回退 settings 拼接。端口取 ``proxy_port``——它是对外端口的
唯一定义处（dev=监听口；容器部署映射两端同值并回注 env，见 docker-compose.yml）。
与 ``placement._distribution_base_url`` 用 Node 连接 Host 回推 origin 同一思路。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.out_models import ProxyEndpointOut
from llmpod.config.settings import settings

router = APIRouter()

# 监听通配地址不能拼进对客地址（不可路由）；Host 出现这些值视为未提供
_UNROUTABLE_HOSTS = frozenset({"0.0.0.0", "::", ""})


def _request_base_url(request: Request) -> str | None:
    """按请求 Host 推导网关基址：Host 主机部分 + 网关端口。

    只取解析后的主机名（剥掉 Host 自带端口），畸形容忍不了则返回 None 交给兜底。
    """
    host = request.url.hostname
    if not host or host.lower() in _UNROUTABLE_HOSTS:
        return None
    if ":" in host:  # IPv6 字面量在 URL 里须加方括号
        host = f"[{host}]"
    scheme = request.url.scheme or "http"  # 随请求方（proxy-headers 开启时尊重 X-Forwarded-Proto，HTTPS 不降级）
    return f"{scheme}://{host}:{settings.proxy_port}"


@router.get("", response_model=Result[ProxyEndpointOut], summary="API 网关接入地址")
async def get_proxy_endpoint(request: Request):
    """返回 Proxyserver 对外接入基址（base_url），供平台端/租户端「模型中心/接入文档」展示。

    未配置宣告地址时按调用方 Host 推导（见模块 docstring）；同机直连得
    ``http://localhost:{proxy_port}``、跨机得对方可路由地址。经反代以独立域名/IP
    暴露网关的部署须显式配 ``proxy_advertise_url``。
    """
    base_url = settings.proxy_advertise_url.rstrip("/") or _request_base_url(request)
    if not base_url:
        base_url = f"http://{settings.proxy_host}:{settings.proxy_port}"
    return success(
        ProxyEndpointOut(
            base_url=base_url,
            advertise_url_configured=bool(settings.proxy_advertise_url),
        )
    )
