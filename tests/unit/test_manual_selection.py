"""手动选卡单测：_validate_manual_selection + GpuScheduler 指定集路径 + validate_topology 规则。

零 DB：FakeLedger mock；GpuCandidate/InstanceSpec/Topology 直接构造。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import (
    GpuCandidate,
    GpuSelector,
    Topology,
)
from llmpod.apps.manager.deployment.scheduler.gpu import (
    GpuScheduler,
    _validate_manual_selection,
    _validate_node_selection,
    pair_manual_replicas,
)
from llmpod.apps.manager.deployment.scheduler.topology import (
    expand_standard,
    validate_topology,
)
from llmpod.core.exceptions import LLMPodValidationError


def _cand(uuid, node, arch="nvidia", total=24.0, status="online", index=0):
    return GpuCandidate(
        uuid=uuid,
        node_id=node,
        index=index,
        arch=arch,
        total_vram_gb=total,
        status=status,
    )


class _FakeLedger:
    def __init__(self, avail_by_uuid=None):
        self.avail = avail_by_uuid or {}
        self.reserved = []

    def get_available(self, uuid, total):
        return self.avail.get(uuid, total)

    async def reserve(self, deployment_id, uuid, vram_gb, total_vram_gb):
        self.reserved.append(uuid)
        return True

    async def force_reserve(self, deployment_id, uuid, vram_gb):
        self.reserved.append(uuid)

    async def release_cards(self, deployment_id, gpu_uuids):
        self.reserved = [u for u in self.reserved if u not in set(gpu_uuids)]

    async def release(self, deployment_id):
        self.reserved.clear()


# ---- _validate_manual_selection ----


class TestValidateManualSelection:
    def test_single_node_ok(self):
        pool = [_cand("g0", "n1"), _cand("g1", "n1")]
        _validate_manual_selection(pool, ["g0", "g1"], tp=2, nnodes=1, arch="nvidia")

    def test_cross_node_ok(self):
        pool = [_cand("g0", "n1"), _cand("g1", "n2")]
        _validate_manual_selection(pool, ["g0", "g1"], tp=2, nnodes=2, arch="nvidia")

    def test_wrong_count(self):
        pool = [_cand("g0", "n1"), _cand("g1", "n1")]
        with pytest.raises(LLMPodValidationError, match="2 张"):
            _validate_manual_selection(pool, ["g0"], tp=2, nnodes=1, arch="nvidia")

    def test_duplicate(self):
        pool = [_cand("g0", "n1"), _cand("g1", "n1")]
        with pytest.raises(LLMPodValidationError, match="重复"):
            _validate_manual_selection(pool, ["g0", "g0"], tp=2, nnodes=1, arch="nvidia")

    def test_wrong_arch(self):
        pool = [_cand("g0", "n1", arch="cann"), _cand("g1", "n1", arch="cann")]
        with pytest.raises(LLMPodValidationError, match="架构不符"):
            _validate_manual_selection(pool, ["g0", "g1"], tp=2, nnodes=1, arch="nvidia")

    def test_offline(self):
        pool = [_cand("g0", "n1", status="offline"), _cand("g1", "n1")]
        with pytest.raises(LLMPodValidationError, match="不可用"):
            _validate_manual_selection(pool, ["g0", "g1"], tp=2, nnodes=1, arch="nvidia")

    def test_cross_node_bad_distribution(self):
        # tp=2 nnodes=2 但两张卡同节点 → 需 2 个节点
        pool = [_cand("g0", "n1"), _cand("g1", "n1")]
        with pytest.raises(LLMPodValidationError, match="2 个节点"):
            _validate_manual_selection(pool, ["g0", "g1"], tp=2, nnodes=2, arch="nvidia")

    def test_cross_node_uneven(self):
        # tp=4 nnodes=2，每节点需 2 张；实际 3+1
        pool = [_cand("g0", "n1"), _cand("g1", "n1"), _cand("g2", "n1"), _cand("g3", "n2")]
        with pytest.raises(LLMPodValidationError, match="每节点需 2 张"):
            _validate_manual_selection(pool, ["g0", "g1", "g2", "g3"], tp=4, nnodes=2, arch="nvidia")


# ---- GpuScheduler.assign 指定集路径 ----


class TestSchedulerManualAssign:
    async def test_single_node_manual(self):
        instances = expand_standard(Topology(tp=2, nnodes=1, replicas=1))
        pool = [_cand("g0", "n1", index=0), _cand("g1", "n1", index=1)]
        sel = GpuSelector(gpu_uuids=["g0", "g1"])
        sched = GpuScheduler(_FakeLedger())
        assignments = await sched.assign("dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=sel)
        assert len(assignments) == 1
        assert set(assignments[0].gpu_uuids) == {"g0", "g1"}
        assert assignments[0].node_id == "n1"

    async def test_cross_node_manual(self):
        instances = expand_standard(Topology(tp=2, nnodes=2, replicas=1))
        pool = [_cand("g0", "n1", index=0), _cand("g1", "n2", index=0)]
        sel = GpuSelector(gpu_uuids=["g0", "g1"])
        sched = GpuScheduler(_FakeLedger())
        assignments = await sched.assign("dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=sel)
        assert len(assignments) == 2
        nodes = {a.node_id for a in assignments}
        assert nodes == {"n1", "n2"}  # 两个 instance 落在不同节点
        all_uuids = {u for a in assignments for u in a.gpu_uuids}
        assert all_uuids == {"g0", "g1"}

    async def test_manual_vram_insufficient_rolls_back(self):
        instances = expand_standard(Topology(tp=2, nnodes=1, replicas=1))
        pool = [_cand("g0", "n1"), _cand("g1", "n1")]
        sel = GpuSelector(gpu_uuids=["g0", "g1"])
        # g1 可用显存不足 → reserve 前的 get_available 判定失败
        ledger = _FakeLedger(avail_by_uuid={"g1": 5.0})
        sched = GpuScheduler(ledger)
        with pytest.raises(LLMPodValidationError, match="显存不足"):
            await sched.assign("dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=sel)

    async def test_manual_force_bypasses_vram_veto(self):
        """force=true：手选卡显存不足仍强制调度（跳过容量过滤 + force_reserve），不静默换卡。"""
        instances = expand_standard(Topology(tp=2, nnodes=1, replicas=1))
        pool = [_cand("g0", "n1"), _cand("g1", "n1")]
        sel = GpuSelector(gpu_uuids=["g0", "g1"])
        ledger = _FakeLedger(avail_by_uuid={"g1": 5.0})  # g1 显存不足，force 下仍调度
        sched = GpuScheduler(ledger)
        assignments = await sched.assign(
            "dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=sel, force=True
        )
        assert len(assignments) == 1
        assert set(assignments[0].gpu_uuids) == {"g0", "g1"}

    async def test_auto_path_still_works(self):
        """无 gpu_selector 时仍走自动调度。"""
        instances = expand_standard(Topology(tp=2, nnodes=1, replicas=1))
        pool = [_cand("g0", "n1", index=0), _cand("g1", "n1", index=1)]
        sched = GpuScheduler(_FakeLedger())
        assignments = await sched.assign("dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=None)
        assert len(assignments) == 1
        assert len(assignments[0].gpu_uuids) == 2

    async def test_multi_replica_manual_assign(self):
        """多副本手选（tp=1×2 份，两实例各 replica_idx 0/1）：预校验按份数计卡不再误拒。"""
        instances = expand_standard(Topology(tp=1, nnodes=1, replicas=2))
        assert len(instances) == 2
        assert {i.replica_idx for i in instances} == {0, 1}
        pool = [_cand("g0", "n1", index=0), _cand("g1", "n1", index=1)]
        sel = GpuSelector(gpu_uuids=["g0", "g1"])
        sched = GpuScheduler(_FakeLedger())
        assignments = await sched.assign("dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=sel)
        assert len(assignments) == 2
        # 每份一张卡，都落在 n1
        assert all(a.node_id == "n1" for a in assignments)
        assert {a.gpu_uuids[0] for a in assignments} == {"g0", "g1"}

    async def test_multi_replica_mixed_arch_manual_assign(self):
        """异构多副本手选（1 cann 份 + 2 nvidia 份，tp=1×3）：份间可混 arch，各份各落。

        复刻用户场景：3 副本手选 1×cann + 2×nvidia——预校验/Filter 链不按
        部署单 arch 否决，实例 gpu_arch 按实际调度到的卡逐份推导（cann 份解析 cann 镜像）。
        """
        instances = expand_standard(Topology(tp=1, nnodes=1, replicas=3))
        pool = [
            _cand("c1", "ncann", arch="cann", index=0),
            _cand("g1", "nnvid", arch="nvidia", index=0),
            _cand("g2", "nnvid", arch="nvidia", index=1),
        ]
        sel = GpuSelector(gpu_uuids=["c1", "g1", "g2"])
        sched = GpuScheduler(_FakeLedger())
        assignments = await sched.assign("dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=sel)
        assert len(assignments) == 3
        # 逐实例 arch 随调度到的卡：cann 份 cann、nvidia 份 nvidia
        arch_by_uuid = {a.gpu_uuids[0]: i.gpu_arch for a, i in zip(assignments, instances)}
        assert arch_by_uuid["c1"] == "cann"
        assert arch_by_uuid["g1"] == "nvidia"
        assert arch_by_uuid["g2"] == "nvidia"

    def test_pair_manual_replicas_mixed_arch(self):
        """配对函数不预过滤 arch：1 cann + 2 nvidia → 3 份各归各桶。"""
        pool = [
            _cand("c1", "ncann", arch="cann"),
            _cand("g1", "nnvid", arch="nvidia"),
            _cand("g2", "nnvid", arch="nvidia"),
        ]
        pairs = pair_manual_replicas(pool, ["c1", "g1", "g2"], per_copy=1)
        assert len(pairs) == 3
        by_arch: dict[str, int] = {}
        for arch, uuids in pairs:
            by_arch[arch] = by_arch.get(arch, 0) + len(uuids)
        assert by_arch == {"cann": 1, "nvidia": 2}

    def test_pair_mixed_keeps_taking_from_largest_bucket(self):
        """「最大桶」按桶总量排、不按剩余量排的语义锁定。

        A 桶 4 卡、B 桶 2 卡，两份各 2 卡：两份都从 A 取（总量序稳定指向同桶；
        若误按剩余量排，第二份时 A 剩 2 = B 剩 2，选择会退化为 node 序——结果
        虽常相同，语义应显式固定）。B 桶配不进 2 卡份 → 余卡拒（调用方不传多余卡）。
        """
        pool = [
            _cand("a1", "nodeA", arch="nvidia"),
            _cand("a2", "nodeA", arch="nvidia"),
            _cand("a3", "nodeA", arch="nvidia"),
            _cand("a4", "nodeA", arch="nvidia"),
            _cand("b1", "nodeB", arch="nvidia"),
            _cand("b2", "nodeB", arch="nvidia"),
        ]
        pairs = pair_manual_replicas(pool, ["a1", "a2", "a3", "a4"], per_copy=[2, 2])
        assert set(pairs[0][1]) == {"a1", "a2"}
        assert set(pairs[1][1]) == {"a3", "a4"}
        # 传进 B 的卡配不进任何份 → 整批拒（不静默丢卡）
        with pytest.raises(LLMPodValidationError, match="余卡"):
            pair_manual_replicas(pool, ["a1", "a2", "a3", "a4", "b1", "b2"], per_copy=[2, 2])


# ---- validate_topology 手动选卡规则 ----


class TestValidateTopologyManual:
    def test_gpu_uuids_cross_node_allowed(self):
        """跨机 + 手动选卡：不再拒绝（本 WP 解锁）。"""
        validate_topology(Topology(tp=2, nnodes=2, replicas=1), GpuSelector(gpu_uuids=["g0", "g1"]))

    def test_gpu_uuids_single_node_multi_replica_allowed(self):
        """单机多副本手选已放开（每份卡同节点同 arch，分组在 pair_manual_replicas）。"""
        validate_topology(Topology(tp=2, replicas=2), GpuSelector(gpu_uuids=["g0", "g1", "g2", "g3"]))

    def test_gpu_uuids_multinode_multi_replica_rejected(self):
        """多副本×跨机×手选拒绝（配对组合爆炸，走自动调度）。"""
        with pytest.raises(LLMPodValidationError, match="多副本×跨机"):
            validate_topology(Topology(tp=2, replicas=2, nnodes=2), GpuSelector(gpu_uuids=["g0", "g1", "g2", "g3"]))

    def test_node_id_cross_node_rejected(self):
        with pytest.raises(LLMPodValidationError, match="指定单个节点"):
            validate_topology(Topology(tp=2, nnodes=2), GpuSelector(node_id="n1"))

    def test_uuids_and_node_mutex(self):
        with pytest.raises(LLMPodValidationError, match="互斥"):
            validate_topology(Topology(tp=2), GpuSelector(node_id="n1", gpu_uuids=["g0", "g1"]))


class TestNodeIdsSelection:
    """跨机选节点集（node_ids）：每节点自动分 tp//nnodes 张卡。"""

    def test_validate_len_equals_nnodes(self):
        validate_topology(Topology(tp=2, nnodes=2, replicas=1), GpuSelector(node_ids=["n1", "n2"]))

    def test_validate_len_mismatch_rejected(self):
        with pytest.raises(LLMPodValidationError, match="node_ids 数量"):
            validate_topology(Topology(tp=2, nnodes=2), GpuSelector(node_ids=["n1"]))

    def test_validate_mutex_with_node_id(self):
        with pytest.raises(LLMPodValidationError, match="互斥"):
            validate_topology(Topology(tp=1), GpuSelector(node_id="n1", node_ids=["n1"]))

    def test_validate_mutex_with_gpu_uuids(self):
        with pytest.raises(LLMPodValidationError, match="互斥"):
            validate_topology(Topology(tp=1), GpuSelector(gpu_uuids=["g0"], node_ids=["n1"]))

    async def test_scheduler_cross_node_auto_spread(self):
        """node_ids 跨机：tp=2/nnodes=2 → 两 instance 各落一个选定节点，自动分卡。"""
        instances = expand_standard(Topology(tp=2, nnodes=2, replicas=1))
        pool = [_cand("g0", "n1", index=0), _cand("g1", "n2", index=0)]
        sel = GpuSelector(node_ids=["n1", "n2"])
        sched = GpuScheduler(_FakeLedger())
        assignments = await sched.assign("dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=sel)
        assert len(assignments) == 2
        assert {a.node_id for a in assignments} == {"n1", "n2"}  # 各落一个选定节点

    async def test_scheduler_missing_node_rejected(self):
        """node_ids 含无卡节点 → 预校验拒绝。"""
        instances = expand_standard(Topology(tp=2, nnodes=2, replicas=1))
        pool = [_cand("g0", "n1"), _cand("g1", "n1")]  # 只有 n1 有卡
        sel = GpuSelector(node_ids=["n1", "n2"])  # n2 无卡
        sched = GpuScheduler(_FakeLedger())
        with pytest.raises(LLMPodValidationError, match="不可用节点"):
            await sched.assign("dep", instances, pool, vram_per_gpu_gb=10.0, arch="nvidia", gpu_selector=sel)

    def test_validate_node_selection_short_cards(self):
        """节点在线但卡不足 → 拒。"""
        pool = [_cand("g0", "n1"), _cand("g1", "n2")]  # 每节点 1 张
        with pytest.raises(LLMPodValidationError, match="不足"):
            _validate_node_selection(pool, ["n1", "n2"], tp=4, nnodes=2, arch="nvidia")  # 需 2 张/节点
