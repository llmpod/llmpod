"""互联 IP 可用性判定：manager 选定与引导参数校验共用这一份实现。

Node.ip 的下游消费面（引擎 api_base、多机 rendezvous master_addr 的
``http://{ip}:{port}`` 拼接、metrics target、同 /24 子网校验）全部按 IPv4
无中括号处理，非可用 IPv4 单播地址写进去是确定性断连。webserver 签发引导
参数与 manager $hello 选定都问这里；引导脚本 valid_ipv4 是跨语言的手工镜像
（改口径须三处同步，tests/unit/test_node_addr.py 的清洗用例表是黄金向量）。
"""

from __future__ import annotations

import ipaddress

# "this network" 段（0.0.0.0/8）：is_unspecified 只认 0.0.0.0 一个地址，其余
# 0.x 同样不可路由进 URL，整段剔除（与引导脚本 valid_ipv4 的 [ o[0] -eq 0 ] 对齐）。
_NETWORK_ZERO = ipaddress.ip_network("0.0.0.0/8")


def clean_advertise_ip(value: str | None) -> str:
    """清洗候选地址：去空白，只认可用 IPv4 单播字面量；非法返回空串。"""
    ip = (value or "").strip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    if addr.version != 4:
        return ""
    if (
        addr.is_unspecified
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved  # 含广播 255.255.255.255（同属 240.0.0.0/4）
        or addr in _NETWORK_ZERO
    ):
        return ""
    return ip
