"""hello 期节点互联 IP 选定：显式配置 > 已存合法值 > 自报值兜底。

历史教训（本模块曾有探活协商，已删）：重连 $hello 重跑协商，而探活只证明
「该地址上有台机器活着」，不辨身份——多出口/组网/两侧同号私网段下，一个
manager 侧其实打不通的地址可以胜出并覆盖落库；地址变了不留痕，直到部署跑完
才发现请求打不通。现口径（2026-09 已定案）：

- **机器不替节点猜地址**：addnode 必须显式选中互联 IP（引导参数 node_ip /
  交互菜单 / env LLMPOD_NODE_ADVERTISE_IP 固定），manager 不再做候选探活。
- **沿用首次选定值**：库中已存合法地址不再重协商——可达性是网络属性，重探
  只会引入换址风险，不做一致性校验（自报值与已存值不一致属预期拓扑变化，
  漂移留 log 痕迹但不阻断）。
- **显式配置可重新指定**：ip_explicit 是操作者指令，永远有效（含把已存值重新指定成
  新地址——这是修正错误地址的唯一正规入口），与已存值不同时告警留痕。
- **未显式固定节点收下 + 告警**：无合法已存值且未显式配置（含历史遗留行）时按
  自报值收下并告警；自报值也非法时置空（register_node 落 0.0.0.0 哨兵，且
  下次重连仍按未显式固定处理、告警不止）——已知坏地址不写进 Node.ip：它会把
  api_base、多机 master_addr、metrics target 全带偏，还可能撑破 String(45)
  让注册 flush 直接失败。可达性兜底属部署前置闸门，另做工单，不在此保证。

心跳不重新选定（每 10s 全集群重跑没有意义）；地址沿用由 $hello 查库实现。
地址可用性判定以 core/netaddr.py 为准（webserver 引导参数校验同源）。
"""

from __future__ import annotations

import logging

from llmpod.core.netaddr import clean_advertise_ip
from llmpod.tunnel.schemas.system import NodeReport

log = logging.getLogger("llmpod.apps.manager.node_addr")

# 选定各分支共用的清洗入口（本模块历史名，测试与注释沿引）
_valid_candidate = clean_advertise_ip


def resolve_node_ip(info: NodeReport, stored_ip: str | None) -> str:
    """返回应写入 Node.ip 的地址（纯本地选定，无网络 IO、不抛错）。"""
    self_ip = _valid_candidate(info.ip)
    stored = _valid_candidate(stored_ip)

    # 1. 显式配置：操作者指令优先于已存值（重新指定是修正错误地址的唯一入口）；
    #    固定值过清洗，坏值不采纳、告警后按已存值/自报值继续。
    if info.ip_explicit:
        if self_ip:
            if stored and stored != self_ip:
                log.warning("节点互联 IP 重新指定：%s → %s（显式配置，操作者指令）", stored, self_ip)
            return self_ip
        log.warning("节点显式配置 IP %r 不可用（须为可用 IPv4 单播地址），忽略", info.ip)

    # 2. 已存合法值：沿用首次选定的地址，不重协商；自报值漂移留 info 痕迹
    #    （换网/DHCP/丢 pin env 的可见信号，修正入口 = 上目标机重跑引导脚本重新指定）。
    if stored:
        if self_ip and self_ip != stored:
            log.info(
                "节点自报互联 IP %s 与已存值 %s 不一致，沿用已存值（换网/丢失显式配置所致；修正请重跑引导脚本重新指定）",
                self_ip,
                stored,
            )
        return stored

    # 3. 未显式固定（历史遗留行 / 首次注册未显式配置）：自报值可用则收下并告警，
    #    落库后即成为已存值，后续重连走分支 2；不可用则置空落库。
    if not self_ip:
        log.warning(
            "节点未显式固定互联 IP，且自报值 %r 不可用（须为可用 IPv4 单播地址），置空落库；"
            "请在目标机重跑引导并显式选定互联 IP（node_ip 参数 / 交互菜单）",
            info.ip,
        )
        return ""
    log.warning(
        "节点未显式固定互联 IP（无合法已存值且未显式配置），按自报值 %s 收下；"
        "请在目标机重跑引导并显式选定互联 IP（node_ip 参数 / 交互菜单）",
        self_ip,
    )
    return self_ip
