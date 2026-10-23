"""RankResolver：分布式 rank/master 编号（纯函数）。

填 instance 的 master_addr / master_port（node_rank 由拓扑展开设定）。
按 replica_idx 分组解析：每组的 master_addr = 本组 node_rank==0（head）所在节点 IP，
master_port = default_master_port + replica_idx（host 网络多 replica 同节点防 rendezvous
端口冲突）；单节点组同样自洽（master 即本节点）。
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from typing import Callable

from llmpod.apps.manager.deployment.schemas import InstanceSpec
from llmpod.core.exceptions import LLMPodValidationError


class RankResolver:
    """rank/master 解析器。"""

    @staticmethod
    def fill(
        instances: list[InstanceSpec],
        default_master_port: int,
        get_node_ip: Callable[[str], str] | None = None,
    ) -> None:
        """就地填充 master_port / master_addr（按 replica 分组，各组独立 master）。

        get_node_ip(node_id) -> ip；组内 node_rank==0 的节点 IP 作为该组 master_addr。
        default_master_port 由调用方从适配器传入（唯一定义处：adapter.default_master_port）。
        """
        groups: dict[int, list[InstanceSpec]] = defaultdict(list)
        for inst in instances:
            groups[inst.replica_idx].append(inst)

        for replica_idx, group in groups.items():
            # 组内 head（node_rank==0）节点 IP 作为该组 master
            master_addr = "127.0.0.1"
            for inst in group:
                if inst.node_rank == 0 and inst.assigned_node:
                    if get_node_ip is not None:
                        master_addr = get_node_ip(inst.assigned_node) or master_addr
                    break

            for inst in group:
                if inst.master_port is None:
                    # +replica_idx：host 网络下多 replica 同节点时 rendezvous 端口去冲突
                    inst.master_port = default_master_port + replica_idx
                if inst.master_addr is None:
                    inst.master_addr = master_addr
                # single-node 保证 node_rank 已是 0（topology 展开设定）
                if inst.nnodes <= 1:
                    inst.node_rank = 0


def _subnets(node_ips: dict[str, str], prefix: int = 24) -> set[str]:
    """返回节点 IP 去重后的子网集合（空 / 非法 IP 跳过，不作为同子网证据）。"""
    nets: set[str] = set()
    for ip in node_ips.values():
        if not ip:
            continue
        try:
            nets.add(str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False).network_address))
        except ValueError:
            continue
    return nets


def validate_same_subnet(node_ips: dict[str, str], prefix: int = 24) -> None:
    """多机部署校验：所有节点 advertise IP 须在同一 /prefix 子网（NCCL 需双向 L3 可达）。

    跨子网（含非对称 NAT：node 能连 manager 但 manager/其他 node 连不回来）会静默跑出
    NCCL 连不通的假部署，故这里显式拒绝。
    """
    nets = _subnets(node_ips, prefix)
    if len(nets) > 1:
        raise LLMPodValidationError(
            f"多机部署要求所有节点在同一 /{prefix} 子网（NCCL 双向可达），实际分布在 {sorted(nets)}"
        )


def subnet_warning(node_ips: dict[str, str], prefix: int = 24) -> str | None:
    """返回跨子网告警文案；同子网（或拿不到 IP）→ None。inspect 预览用（仅提醒，不阻断）。"""
    nets = _subnets(node_ips, prefix)
    if len(nets) <= 1:
        return None
    return (
        f"多机节点分布在 {len(nets)} 个 /{prefix} 子网 {sorted(nets)}，NCCL 可能不可达（跨子网部署需 force 才能提交）"
    )
