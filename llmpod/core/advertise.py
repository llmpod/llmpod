"""本机对外地址推导：配置只填 IP/域名，各处的端点地址由此拼。

一个配置值两种写法：

- 裸 host（``10.0.0.5`` / ``mgr.example.com``）→ 补对外端口（``webserver_port``，
  容器部署下容器内外同值）与固定路径，scheme 取 ws / http。这是常态。
- 完整 URL（``wss://mgr.example.com``）→ 反代 + TLS 场景：scheme 原样、端口不补
  （由反代决定），只做 ws↔http 的角色映射。只认 origin，路径前缀不支持（挂在
  子路径下的反代请用域名/子域名接入）。

取值在拼装时校验，非法形态当场抛 ConfigError——废地址拼出来发到目标机，表现为
节点无限重连且无任何报错回传，必须挡在生成侧。

消费方三处，取的都是同一份推导，不在各调用点各写一份拼接：
- 引导脚本里节点回连的 WS 地址（``manager_ws_url``；内网直连候选清单
  ``manager_ws_candidates`` 是它的多地址版，供节点侧探活优选）
- Manager 的 HTTP origin：引导 curl 命令、以及文件服务端点的兜底（``manager_http_origin``）
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from llmpod.config.settings import settings
from llmpod.core.exceptions import ConfigError

# 同一地址的 ws 侧与 http 侧视图：换个 scheme 就是另一种用途
_WS_SCHEMES = {"ws": "ws", "wss": "wss", "http": "ws", "https": "wss"}
_HTTP_SCHEMES = {"ws": "http", "wss": "https", "http": "http", "https": "https"}

# 监听通配地址对外不可路由：照抄进对外地址会让节点拨到自己
_UNROUTABLE_HOSTS = frozenset({"0.0.0.0", "::"})


def _reject(value: str, reason: str) -> None:
    raise ConfigError(f"LLMPOD_NODE_MANAGER_IP 取值非法（{value!r}）：{reason}")


def _check_bare_host(value: str) -> None:
    """裸 host 只认主机名 / 带方括号的 IPv6，其余形态都是误填。"""
    if value in _UNROUTABLE_HOSTS:
        _reject(value, "0.0.0.0/:: 是监听地址不是对外地址，填本机 IP 或域名")
    if "/" in value:
        _reject(value, "不要带路径或斜杠，只填主机名")
    if value.startswith("[") and value.endswith("]"):
        return  # 带方括号的 IPv6 字面量，唯一合法的含多冒号形态
    if value.count(":") >= 2:
        _reject(value, "IPv6 请加方括号，如 [2001:db8::1]")
    if ":" in value:
        _reject(value, "不要带端口（端口由 LLMPOD_WEBSERVER_PORT 决定），只填 IP 或域名")
    # 字符集兜底：杂字符（粘贴带进来的括号/引号等）拼出的地址拨不通，
    # 表现为节点无限重连无报错——在这层就拦成启动期 ConfigError
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?", value):
        _reject(value, "主机名只能含字母/数字/点/连字符（IP 与域名都在此集内）")


def _check_url(value: str) -> None:
    """完整 URL 只认 origin 形态：ws/wss/http/https + host[:port]，不带路径。"""
    parsed = urlparse(value)
    if parsed.scheme not in _WS_SCHEMES:
        _reject(value, "完整 URL 的 scheme 只认 ws/wss/http/https")
    if not parsed.netloc:
        _reject(value, "URL 缺主机部分")
    if parsed.path not in ("", "/"):
        _reject(value, "不支持路径前缀（挂在子路径下的反代请改用域名/子域名接入）")
    host = (parsed.hostname or "").strip("[]").lower()
    if host in _UNROUTABLE_HOSTS:
        _reject(value, "0.0.0.0/:: 是监听地址不是对外地址，填本机 IP 或域名")


def _resolve(value: str, schemes: dict[str, str], default_scheme: str) -> tuple[str, str]:
    """→ (scheme, netloc)，非法取值抛 ConfigError。裸 host 补 webserver_port；完整 URL 原样取 netloc。"""
    value = (value or "").strip()
    if not value:
        # 未配置：localhost 仅同机可用，多机部署由部署侧填真地址
        return default_scheme, f"localhost:{settings.webserver_port}"
    if "://" not in value:
        _check_bare_host(value)
        scheme, netloc = default_scheme, f"{value}:{settings.webserver_port}"
    else:
        _check_url(value)
        parsed = urlparse(value)
        scheme, netloc = schemes[parsed.scheme], parsed.netloc
    return scheme, netloc


def manager_ws_url() -> str:
    """节点回连 Manager 的 WS 地址（渲染进引导脚本 / 节点容器 env）。"""
    scheme, netloc = _resolve(settings.node_manager_ip, _WS_SCHEMES, "ws")
    return f"{scheme}://{netloc}{settings.manager_ws_path}"


def manager_ws_candidates() -> list[str]:
    """引导脚本的回连候选清单：内网直连优先，主地址（对外）兜底压轴。

    LAN 键（``node_manager_ip_lan``，逗号分隔的裸 host）装的是本机全部内网
    地址——装机脚本探测后写入；逐个走与主键同一套校验和拼接。引导脚本在
    节点机上按清单逐个探活（GET /health），选中第一个通的：内网节点拿直连
    路径，全不通落主地址（对外保证可达）。LAN 为空或与主地址重合时清单
    只剩主地址，行为与单地址时代一致。
    """
    primary = manager_ws_url()
    default_scheme = "wss" if primary.startswith("wss://") else "ws"
    candidates: list[str] = []
    for host in settings.node_manager_ip_lan.split(","):
        host = host.strip()
        if not host:
            continue
        scheme, netloc = _resolve(host, _WS_SCHEMES, default_scheme)
        url = f"{scheme}://{netloc}{settings.manager_ws_path}"
        if url not in candidates:
            candidates.append(url)
    if primary not in candidates:
        candidates.append(primary)
    return candidates


def manager_http_origin() -> str:
    """Manager 的 HTTP(S) origin：引导 curl 命令、文件服务端点前缀都用它。"""
    scheme, netloc = _resolve(settings.node_manager_ip, _HTTP_SCHEMES, "http")
    return f"{scheme}://{netloc}"
