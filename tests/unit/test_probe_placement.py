"""probe_placement 单测：inspect 结构性容量探测（无状态、零 DB）。

探测直接干跑选卡内核（``plan_placement``，card_ok 不注入）——只判「这套拓扑在这个
集群按这些架构摆不摆得下」（在线 / 架构 / 同档 / 节点分布），显存够不够由逐卡容量
检查另行回答。因此本文件的期望一律以「内核能不能选出卡」为准。
"""

from __future__ import annotations

from dataclasses import dataclass

from llmpod.apps.manager.deployment.schemas import Topology
from llmpod.apps.manager.deployment.scheduler.gpu import probe_placement
from llmpod.apps.manager.deployment.scheduler.topology import expand_standard


@dataclass
class Card:
    uuid: str
    node_id: str
    index: int = 0
    arch: str = "nvidia"
    total_vram_gb: float = 24.0
    status: str = "online"


def _insts(tp: int = 1, pp: int = 1, replicas: int = 1, nnodes: int = 1):
    return expand_standard(Topology(tp=tp, pp=pp, replicas=replicas, nnodes=nnodes))


# ---- 单节点 ----


def test_single_node_enough():
    pool = [Card("u1", "n1", 0), Card("u2", "n1", 1), Card("u3", "n1", 2)]
    ok, reason = probe_placement(_insts(tp=2), pool, arch="nvidia")
    assert ok and reason is None


def test_single_node_short_counts_online_cards():
    """tp=2 但节点只有 1 张在线卡 → 不够，原因里给「需 1 份引擎（每份 2 张）+ 在线分布」。"""
    pool = [Card("u1", "n1", 0)]
    ok, reason = probe_placement(_insts(tp=2), pool, arch="nvidia")
    assert not ok
    assert "每份 2 张同架构卡" in reason and "nvidia 1 张" in reason


def test_replicas_multiplies_engines():
    """tp=1 × 3 份而只有 2 张卡 → 不够（每份一个引擎，跨引擎卡不共享）。"""
    pool = [Card("u1", "n1", 0), Card("u2", "n1", 1)]
    ok, reason = probe_placement(_insts(replicas=3), pool, arch="nvidia")
    assert not ok
    assert "3 份引擎" in reason and "共 3 张" in reason


def test_pp_counts_into_per_engine_cards():
    pool = [Card("u1", "n1", 0), Card("u2", "n1", 1)]
    ok, _ = probe_placement(_insts(pp=2), pool, arch="nvidia")
    assert ok
    ok, reason = probe_placement(_insts(pp=2), pool[:1], arch="nvidia")
    assert not ok
    assert "每份 2 张同架构卡" in reason


def test_offline_cards_excluded():
    pool = [Card("u1", "n1", 0), Card("u2", "n1", 1, status="offline")]
    ok, reason = probe_placement(_insts(tp=2), pool, arch="nvidia")
    assert not ok
    assert "nvidia 1 张" in reason  # 仅 1 张在线


def test_arch_mismatch_excluded():
    pool = [Card("u1", "n1", 0, arch="nvidia"), Card("u2", "n1", 1, arch="cann")]
    ok, reason = probe_placement(_insts(tp=2), pool, arch="nvidia")
    assert not ok
    assert "nvidia 1 张" in reason and "cann 1 张" in reason


# ---- 架构回落（异构副本）----


def test_arch_order_falls_back_to_other_arch():
    """nvidia 只有 1 张、cann 有 1 张：单架构凑不出 2 份，按 arch_order 回落就够。

    这是分层调度的关键性质——计数口径（「同 arch 在线卡 ≥ 2」）会说不够，实际上
    两份引擎各落一种架构是合法的（副本间欢迎异构）。
    """
    pool = [Card("g1", "n1", 0, arch="nvidia"), Card("c1", "n2", 0, arch="cann")]
    ok, _ = probe_placement(_insts(replicas=2), pool, arch="nvidia")
    assert not ok  # 只看 nvidia：2 份要 2 张
    ok, reason = probe_placement(_insts(replicas=2), pool, arch="nvidia", arch_order=("nvidia", "cann"))
    assert ok and reason is None


def test_arch_order_does_not_mix_within_engine():
    """份内不得跨架构：每份要 tp=2 张，nvidia/cann 各 1 张 → 回落也凑不出。"""
    pool = [Card("g1", "n1", 0, arch="nvidia"), Card("c1", "n2", 0, arch="cann")]
    ok, reason = probe_placement(_insts(tp=2), pool, arch="nvidia", arch_order=("nvidia", "cann"))
    assert not ok
    assert "每份 2 张同架构卡" in reason


# ---- 跨机 ----


def test_cross_node_enough():
    pool = [Card("u1", "n1", 0), Card("u2", "n1", 1), Card("u3", "n2", 0), Card("u4", "n2", 1)]
    ok, reason = probe_placement(_insts(tp=4, nnodes=2), pool, arch="nvidia")
    assert ok and reason is None


def test_cross_node_short_nodes():
    """跨机引擎的分片算同一份（tp×pp 张分布在 nnodes 个节点）——只有 1 个节点够 2 张。"""
    pool = [Card("u1", "n1", 0), Card("u2", "n1", 1), Card("u3", "n2", 0)]
    ok, reason = probe_placement(_insts(tp=4, nnodes=2), pool, arch="nvidia")
    assert not ok
    assert "1 份引擎" in reason and "每份 4 张同架构卡" in reason


def test_empty_pool_short():
    ok, reason = probe_placement(_insts(), [], arch="nvidia")
    assert not ok
    assert "无在线卡" in reason


def test_no_instances_is_vacuously_ok():
    ok, reason = probe_placement([], [], arch="nvidia")
    assert ok and reason is None
