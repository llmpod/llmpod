"""跨机分片的 CPU 架构一致性（调度内核分组 / 手选校验 / 选节点校验 / 集群推荐）。

同一引擎实例的分片共用镜像 ref 与分布式通信栈，amd64/arm64 混排要到拉镜像或
rendezvous 阶段才暴露——本组测试钉住四条拦截路径与 force 放开语义。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from llmpod.apps.manager.deployment.recommend import GpuPoolEntry, pick_cluster
from llmpod.apps.manager.deployment.schemas import InstanceSpec, PlacementPrefs
from llmpod.apps.manager.deployment.scheduler.gpu import (
    _validate_node_selection,
    plan_placement,
    validate_manual_gpu_selection,
)
from llmpod.apps.manager.deployment.scheduler.pipeline import EngineRequest, PipelineRejection, place_engine
from llmpod.apps.manager.deployment.scheduler.rank import validate_same_cpu_arch
from llmpod.core.exceptions import LLMPodValidationError


@dataclass
class Card:
    uuid: str
    node_id: str
    index: int = 0
    arch: str = "nvidia"
    total_vram_gb: float = 24.0
    status: str = "online"
    available_vram_gb: float = 24.0
    cpu_arch: str = "x86_64"


def _pool_x86_arm() -> list[Card]:
    """单卡双节点：n1=x86、n2=arm，规格完全一致——差异只剩 CPU 架构。"""
    return [
        Card("g-x86", "n1", index=0, cpu_arch="x86_64"),
        Card("g-arm", "n2", index=0, cpu_arch="aarch64"),
    ]


def _req(**kw) -> EngineRequest:
    defaults = dict(engine_id="e1", need_per_node=1, nnodes=2, arch="nvidia")
    defaults.update(kw)
    return EngineRequest(**defaults)


class TestCrossNodeGrouping:
    def test_mixed_cluster_never_mixes_shards(self):
        """tp=1×nnodes=2 的混架构集群：分片只能落到同一架构节点组（此处无解）。"""
        with pytest.raises(PipelineRejection) as ei:
            place_engine(_pool_x86_arm(), _req())
        assert "CPU 架构" in (ei.value.user_reason or "")

    def test_same_arch_third_node_unblocks(self):
        """补一个 x86 节点后跨机可行，且两个分片都落在 x86 节点上。"""
        pool = _pool_x86_arm() + [Card("g-x86-2", "n3", index=0, cpu_arch="x86_64")]
        p = place_engine(pool, _req())
        assert {nid for nid, _ in p.shards} == {"n1", "n3"}

    def test_allow_mixed_cpu_arch_falls_back_to_single_pool(self):
        """force 语义（allow_mixed_cpu_arch）：混架构节点照旧组合，由用户自担。"""
        p = place_engine(_pool_x86_arm(), _req(allow_mixed_cpu_arch=True))
        assert {nid for nid, _ in p.shards} == {"n1", "n2"}

    def test_unknown_cpu_arch_joins_any_group(self):
        """未上报架构的节点与任意已知架构组兼容（缺数据不卡）。"""
        pool = _pool_x86_arm() + [Card("g-unknown", "n3", index=0, cpu_arch="")]
        p = place_engine(pool, _req())
        assert "n2" not in {nid for nid, _ in p.shards}

    def test_mixed_rejection_carries_note(self):
        """落空归因带 cpu_arch_note（inspect 探针据此区分「卡不足」与「混架构」）。"""
        with pytest.raises(PipelineRejection) as ei:
            place_engine(_pool_x86_arm(), _req())
        assert ei.value.cpu_arch_note and "x86_64" in ei.value.cpu_arch_note and "aarch64" in ei.value.cpu_arch_note

    def test_shortage_without_mix_gets_no_note(self):
        """真缺节点（无混架构可凑）时不归因混架构——只有一个已知架构。"""
        pool = [Card("g1", "n1", cpu_arch="x86_64")]
        with pytest.raises(PipelineRejection) as ei:
            place_engine(pool, _req())
        assert ei.value.cpu_arch_note is None


class TestManualSelectionCpuArch:
    def test_cross_node_manual_mixed_rejected(self):
        """手选卡跨机混架构（n1 x86 + n2 arm 各一张）→ 结构校验硬拒。"""
        pool = _pool_x86_arm()
        with pytest.raises(LLMPodValidationError, match="CPU 架构不一致"):
            validate_manual_gpu_selection(pool, ["g-x86", "g-arm"], shapes=[(2, 1, 2)], arch="nvidia")

    def test_cross_node_manual_same_arch_passes(self):
        """跨机手选同架构节点（n1/n3 均 x86_64）各一张 → 放行。"""
        pool = _pool_x86_arm() + [Card("g-x86-2", "n3", cpu_arch="x86_64")]
        validate_manual_gpu_selection(pool, ["g-x86", "g-x86-2"], shapes=[(2, 1, 2)], arch="nvidia")

    def test_kernel_manual_mixed_rejected(self):
        """内核手选路径同样拒混架构（校验层之外的兜底；与 gpu.py 校验同语义：
        已知架构 ≥2 即拒，未上报节点不参与判定）。"""
        with pytest.raises(LLMPodValidationError, match="CPU 架构不一致"):
            place_engine(
                _pool_x86_arm(),
                _req(wanted_uuids=("g-x86", "g-arm")),
            )

    def test_kernel_manual_mixed_force_allowed(self):
        """内核手选路径 force 旗标（allow_mixed_cpu_arch）放行混架构。"""
        p = place_engine(_pool_x86_arm(), _req(wanted_uuids=("g-x86", "g-arm"), allow_mixed_cpu_arch=True))
        assert {nid for nid, _ in p.shards} == {"n1", "n2"}

    def test_unknown_arch_manual_not_blocked(self):
        """未上报架构的节点不拦（缺数据不卡）。"""
        pool = [Card("g1", "n1", cpu_arch="x86_64"), Card("g2", "n2", cpu_arch="")]
        validate_manual_gpu_selection(pool, ["g1", "g2"], shapes=[(2, 1, 2)], arch="nvidia")

    def test_force_skips_cpu_arch_check(self):
        """check_cpu_arch=False（force 部署）：混架构手选照常过校验。"""
        pool = _pool_x86_arm()
        validate_manual_gpu_selection(
            pool, ["g-x86", "g-arm"], shapes=[(2, 1, 2)], arch="nvidia", check_cpu_arch=False
        )

    def test_single_node_manual_mixed_arch_copies_allowed(self):
        """单机多副本份间可异构 CPU 架构（各份独立引擎、镜像按份解析），不拦。"""
        pool = _pool_x86_arm()
        # 2 份 tp=1 单机副本：每份同节点同架构即合法
        validate_manual_gpu_selection(pool, ["g-x86", "g-arm"], shapes=[(1, 1, 1), (1, 1, 1)], arch="nvidia")


class TestNodeSelectionCpuArch:
    def test_cross_node_node_ids_mixed_rejected(self):
        pool = _pool_x86_arm()
        with pytest.raises(LLMPodValidationError, match="CPU架构不一致"):
            _validate_node_selection(pool, ["n1", "n2"], tp=2, nnodes=2, arch="nvidia")

    def test_node_ids_with_enough_same_arch_pass(self):
        """选节点集比需要的大：存在同架构凑得出 nnodes 的子集即放行。"""
        pool = _pool_x86_arm() + [Card("g-x86-2", "n3", cpu_arch="x86_64")]
        _validate_node_selection(pool, ["n1", "n2", "n3"], tp=2, nnodes=2, arch="nvidia")

    def test_node_ids_force_skips(self):
        pool = _pool_x86_arm()
        _validate_node_selection(pool, ["n1", "n2"], tp=2, nnodes=2, arch="nvidia", check_cpu_arch=False)


class TestPostAssignGuard:
    def test_validate_same_cpu_arch_rejects_mix(self):
        with pytest.raises(LLMPodValidationError, match="CPU 架构"):
            validate_same_cpu_arch({"n1": "x86_64", "n2": "aarch64"})

    def test_validate_same_cpu_arch_ignores_unknown(self):
        validate_same_cpu_arch({"n1": "x86_64", "n2": ""})
        validate_same_cpu_arch({"n1": "", "n2": ""})


class TestClusterRecommendation:
    # 卡规格取 48G：跨机分支按每卡需求（vram÷tp）判容量，24G 卡按整卡 0.9 预留装不下 24G 需求
    def _pools(self, cpu_n1: str, cpu_n2: str) -> dict[int, list[GpuPoolEntry]]:
        return {
            1: [
                GpuPoolEntry("g1", "n1", "nvidia", 48.0, 48.0, cpu_arch=cpu_n1),
                GpuPoolEntry("g2", "n2", "nvidia", 48.0, 48.0, cpu_arch=cpu_n2),
            ]
        }

    def test_cross_node_recommend_requires_same_arch_group(self):
        """tp=2 两节点各 1 张但横跨 x86/arm：跨机推荐不再放行（旧口径 len≥tp 会放）。"""
        assert pick_cluster(self._pools("x86_64", "aarch64"), 48.0, tp=2) is None

    def test_cross_node_recommend_same_arch_ok(self):
        pick = pick_cluster(self._pools("x86_64", "x86_64"), 48.0, tp=2)
        assert pick is not None and pick.cross_node

    def test_unknown_arch_counts_into_any_group(self):
        # 未上报节点可入任一组：x86(1) + 未知(1) 凑 tp=2 可行
        pools = self._pools("x86_64", "")
        pick = pick_cluster(pools, 48.0, tp=2)
        assert pick is not None and pick.cross_node


class TestFoldWiring:
    def test_plan_placement_threads_allow_mixed(self):
        """prefs.allow_mixed_cpu_arch 经 plan_placement 折叠进内核（force 全链路贯通）。"""
        instances = [
            InstanceSpec(
                model_path="/m", port=8000, task="chat", gpu_arch="nvidia",
                tp=2, pp=1, nnodes=2, node_rank=r, rank=r, replica_idx=0,
                engine_instance_id="e1",
            )
            for r in range(2)
        ]
        # 默认（非 force）：混架构无解
        with pytest.raises(LLMPodValidationError):
            plan_placement(instances, _pool_x86_arm(), arch="nvidia")
        # force：放行混排
        plan = plan_placement(
            instances, _pool_x86_arm(), arch="nvidia", prefs=PlacementPrefs(allow_mixed_cpu_arch=True)
        )
        assert {node_id for _, node_id, _ in plan} == {"n1", "n2"}


class TestAssignForceWiring:
    async def test_assign_force_allows_mixed_manual_selection(self):
        """assign(force=True) 的完整接线：prefs 置位 → 手选校验跳过 → 内核放行混架构。

        这是 create 侧「force 可绕」的端到端钉子（校验层与内核层共用 prefs 一个开关）。
        """
        from llmpod.apps.manager.deployment.resource_ledger import ResourceLedger
        from llmpod.apps.manager.deployment.schemas import GpuCandidate, GpuSelector
        from llmpod.apps.manager.deployment.scheduler.gpu import GpuScheduler

        pool = [
            GpuCandidate(uuid="g-x86", node_id="n1", index=0, arch="nvidia", total_vram_gb=24.0, status="online", cpu_arch="x86_64"),
            GpuCandidate(uuid="g-arm", node_id="n2", index=0, arch="nvidia", total_vram_gb=24.0, status="online", cpu_arch="aarch64"),
        ]
        insts = [InstanceSpec(tp=2, pp=1, nnodes=2, node_rank=r, rank=r, replica_idx=0, engine_instance_id="e1") for r in range(2)]
        selector = GpuSelector(gpu_uuids=["g-x86", "g-arm"])
        # 非 force：手选校验先拒
        with pytest.raises(LLMPodValidationError, match="CPU 架构不一致"):
            await GpuScheduler(ResourceLedger()).assign(
                "dep-mixed", insts, pool, 8.0, arch="nvidia", gpu_selector=selector
            )
        # force：校验跳过 + 内核单池组合 + force_reserve，落位混架构两节点
        insts = [InstanceSpec(tp=2, pp=1, nnodes=2, node_rank=r, rank=r, replica_idx=0, engine_instance_id="e1") for r in range(2)]
        await GpuScheduler(ResourceLedger()).assign(
            "dep-mixed-2", insts, pool, 8.0, arch="nvidia", gpu_selector=selector, force=True
        )
        assert {i.assigned_node for i in insts} == {"n1", "n2"}
