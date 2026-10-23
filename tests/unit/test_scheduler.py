"""调度器三件单测：expand_standard / GpuScheduler / RankResolver。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import GpuCandidate, GpuSelector, InstanceSpec, Topology
from llmpod.apps.manager.deployment.scheduler.topology import expand_standard, validate_topology
from llmpod.apps.manager.deployment.scheduler.gpu import GpuScheduler, recommend_placement
from llmpod.apps.manager.deployment.scheduler.rank import RankResolver
from llmpod.apps.manager.deployment.resource_ledger import ResourceLedger
from llmpod.apps.webserver.schemas.deployment_inspect import ClusterGpuInfo
from llmpod.core.exceptions import LLMPodValidationError


class TestExpandStandard:
    def test_single_replica(self):
        insts = expand_standard(Topology(mode="tensor_parallel", tp=4, replicas=1))
        assert len(insts) == 1
        assert insts[0].tp == 4
        assert insts[0].role == "full"
        assert insts[0].nnodes == 1  # v1 single-node per instance
        assert insts[0].node_rank == 0

    def test_multi_replica(self):
        insts = expand_standard(Topology(tp=1, replicas=3))
        assert len(insts) == 3
        assert [i.replica_idx for i in insts] == [0, 1, 2]

    def test_defaults(self):
        insts = expand_standard(Topology())
        assert len(insts) == 1
        assert insts[0].tp == 1

    def test_via_adapter_hook(self):
        """编排器走 adapter.expand_topology(topology)（基类默认 → expand_standard）。"""
        from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter

        insts = VLLMAdapter.expand_topology(Topology(tp=2, replicas=1))
        assert len(insts) == 1 and insts[0].tp == 2

    def test_multi_node_expansion(self):
        """nnodes>1：每 replica 展开 head + workers，tp 保持全局值（每节点卡数由调度器均分）。"""
        insts = expand_standard(Topology(tp=4, nnodes=2, replicas=1))
        assert len(insts) == 2
        assert [i.node_rank for i in insts] == [0, 1]
        assert [i.role for i in insts] == ["head", "worker"]
        assert all(i.tp == 4 and i.nnodes == 2 for i in insts)
        assert all(i.replica_idx == 0 for i in insts)


class TestValidateTopology:
    def test_nnodes_default_ok(self):
        validate_topology(Topology(tp=4))  # 默认 nnodes=1 合法

    def test_multi_node_ok(self):
        validate_topology(Topology(tp=4, nnodes=2))

    def test_nnodes_zero_raises(self):
        with pytest.raises(LLMPodValidationError):
            validate_topology(Topology(tp=4, nnodes=0))

    def test_tp_not_divisible_by_nnodes_raises(self):
        with pytest.raises(LLMPodValidationError):
            validate_topology(Topology(tp=3, nnodes=2))

    def test_multi_replica_multi_node_now_allowed(self):
        """多机×多副本随四层模型解锁（每副本独立落 nnodes 个不同节点）。"""
        validate_topology(Topology(tp=4, nnodes=2, replicas=2))  # 不再 raise

    def test_gpu_selector_multi_node_raises(self):
        with pytest.raises(LLMPodValidationError):
            validate_topology(Topology(tp=4, nnodes=2), gpu_selector=GpuSelector(node_id="n1"))

    def test_expand_rejects_invalid(self):
        """expand_standard 入口同样拒绝（_plan 路径兜底）。"""
        with pytest.raises(LLMPodValidationError):
            expand_standard(Topology(tp=3, nnodes=2))

    def test_nnodes_spec_round_trip(self):
        """nnodes 经 DeploymentConfig spec JSONB round-trip（落库/读出补默认）。"""
        from llmpod.apps.manager.deployment.schemas import DeploymentConfig

        cfg = DeploymentConfig(topology=Topology(tp=4, nnodes=2))
        dumped = cfg.model_dump()
        assert dumped["topology"]["nnodes"] == 2
        restored = DeploymentConfig.model_validate(dumped)
        assert restored.topology.nnodes == 2
        # 旧 spec 无 nnodes 键 → 默认 1（存量部署零影响）
        assert DeploymentConfig.model_validate({"topology": {"tp": 2}}).topology.nnodes == 1


class TestGpuScheduler:
    @pytest.fixture
    def gpu_pool(self):
        return [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n1", index=1, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g2", node_id="n2", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g3", node_id="n1", index=2, arch="cann", total_vram_gb=16.0, status="online"),  # 异构
            GpuCandidate(uuid="g4", node_id="n1", index=3, arch="nvidia", total_vram_gb=24.0, status="offline"),  # 离线
        ]

    async def test_assign_single_node_tp2(self, gpu_pool):
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        insts = [InstanceSpec(instance_id="i1", tp=2)]
        result = await sched.assign("dep1", insts, gpu_pool, vram_per_gpu_gb=8.0, arch="nvidia")
        assert insts[0].assigned_node == "n1"
        assert insts[0].assigned_gpus == [0, 1]  # 节点 n1 前两个 nvidia
        assert len(result) == 1
        assert set(result[0].gpu_uuids) == {"g0", "g1"}

    async def test_assign_insufficient_raises(self):
        """无节点有 tp 个同架构 GPU → 抛 LLMPodValidationError。"""
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        # 两个节点各只有 1 个 nvidia GPU → tp=2 无节点满足
        single_pool = [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n2", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
        ]
        with pytest.raises(LLMPodValidationError):
            await sched.assign(
                "dep1", [InstanceSpec(instance_id="i1", tp=2)], single_pool, vram_per_gpu_gb=8.0, arch="nvidia"
            )

    async def test_assign_vram_over_capacity_raises(self, gpu_pool):
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        # 每卡要 25GB，但容量 24*0.9=21.6 → reserve 失败
        with pytest.raises(LLMPodValidationError):
            await sched.assign(
                "dep1", [InstanceSpec(instance_id="i1", tp=1)], gpu_pool, vram_per_gpu_gb=25.0, arch="nvidia"
            )

    async def test_assign_force_bypasses_vram_capacity(self, gpu_pool):
        """force=true：显存不足（占比上限装不下模型）仍强制调度 + force_reserve，不抛容量错误。"""
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        insts = [InstanceSpec(instance_id="i1", tp=1)]
        await sched.assign("dep1", insts, gpu_pool, vram_per_gpu_gb=25.0, arch="nvidia", force=True)
        # binpack 默认仍选最紧的 n2/g2（容量过滤被跳过，但选位内核不变）
        assert insts[0].assigned_gpu_uuids == ["g2"]
        # force_reserve 不计容量校验：预留照记（超额由引擎/NCCL 兜底）
        reserved = ledger.get_reserved_for_deployment("dep1")
        assert set(reserved) == {"g2"}
        assert reserved["g2"] == pytest.approx(24.0 * 0.9)

    async def test_arch_filter(self, gpu_pool):
        """只分配同架构 GPU（cann 卡不被 nvidia 部署选中）。

        binpack 默认：n2 仅 1 张可用（n1 有 2 张）→ 选更紧的 n2/g2。
        """
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        insts = [InstanceSpec(instance_id="i1", tp=1)]
        await sched.assign("dep1", insts, gpu_pool, vram_per_gpu_gb=8.0, arch="nvidia")
        assert insts[0].assigned_gpu_uuids == ["g2"]  # 不是 cann 的 g3，也不是离线的 g4

    async def test_gpu_selector_targets_node(self, gpu_pool):
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        insts = [InstanceSpec(instance_id="i1", tp=1)]
        await sched.assign(
            "dep1", insts, gpu_pool, vram_per_gpu_gb=8.0, arch="nvidia", gpu_selector=GpuSelector(node_id="n2")
        )
        assert insts[0].assigned_node == "n2"

    async def test_assign_skips_vram_full_gpu_picks_next(self, gpu_pool):
        """候选按可用 VRAM 过滤——低 index 但近满的卡应跳过选下一张，而非 reserve 失败即 raise。

        g0 已被另一部署预留近满（avail≈1.6GB），g1 全空。新部署 tp=1 需 8GB：
        旧逻辑盲选 g0 → reserve 失败 → 误拒；修复后跳过 g0 选 g1。
        """
        ledger = ResourceLedger()
        await ledger.reserve("other-dep", "g0", 20.0, 24.0)  # g0 avail = 24*0.9-20 = 1.6GB
        sched = GpuScheduler(ledger)
        insts = [InstanceSpec(instance_id="i1", tp=1)]
        await sched.assign("dep1", insts, gpu_pool, vram_per_gpu_gb=8.0, arch="nvidia")
        assert insts[0].assigned_gpu_uuids == ["g1"]  # 跳过近满的 g0

    async def test_assign_two_deployments_use_distinct_gpus(self, gpu_pool):
        """两个 tp=1 小模型（无 vram_percent）应独占各自 GPU，而非堆同一张卡。

        根因：默认预留用模型估算（vram_per_gpu_gb），而 vLLM 实际按 util 抢占整卡；
        账本误判 g0 还有余量，把第二个部署也塞进去 → 实际 OOM。
        binpack 默认：dep1 先用最紧的 n2/g2，dep2 落 n1/g0——仍互不重叠。
        """
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        i1 = [InstanceSpec(instance_id="i1", tp=1)]
        await sched.assign("dep1", i1, gpu_pool, vram_per_gpu_gb=8.0, arch="nvidia")
        assert i1[0].assigned_gpu_uuids == ["g2"]
        # dep1 独占 g2：预留 = total×util（24×0.9=21.6），而非模型估算 8.0
        assert ledger.get_available("g2", 24.0) == 0.0

        i2 = [InstanceSpec(instance_id="i2", tp=1)]
        await sched.assign("dep2", i2, gpu_pool, vram_per_gpu_gb=8.0, arch="nvidia")
        assert i2[0].assigned_gpu_uuids == ["g0"]  # 不堆 g2，也不碰 cann/离线卡

    async def test_assign_multi_node_distinct_nodes(self):
        """跨机：每节点 tp//nnodes 卡，同 replica 实例落在不同节点。"""
        pool = [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n1", index=1, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g2", node_id="n2", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g3", node_id="n2", index=1, arch="nvidia", total_vram_gb=24.0, status="online"),
        ]
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        insts = [
            InstanceSpec(instance_id="i0", tp=4, nnodes=2, node_rank=0, replica_idx=0),
            InstanceSpec(instance_id="i1", tp=4, nnodes=2, node_rank=1, replica_idx=0),
        ]
        await sched.assign("dep1", insts, pool, vram_per_gpu_gb=8.0, arch="nvidia")
        assert insts[0].assigned_gpus == [0, 1]  # 每节点 tp//nnodes=2 卡
        assert insts[1].assigned_gpus == [0, 1]
        assert {insts[0].assigned_node, insts[1].assigned_node} == {"n1", "n2"}  # 不同节点

    async def test_manual_heterogeneous_order_independent(self):
        """手选 + 异构副本：调度与配对校验同一「大份优先」贪心，同一选集不因 copies 填写顺序而变。

        回归：旧调度按声明序逐份取最大桶——copies=[{tp:1},{tp:2}] 时小份先抓走 n2
        桶里的 1 张，大份（需同节点 2 张）配不齐直接拒；把两份写反就能成功。
        """
        from llmpod.apps.manager.deployment.scheduler.gpu import plan_placement

        pool = [
            GpuCandidate(uuid="c0", node_id="n1", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="c1", node_id="n2", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="c2", node_id="n2", index=1, arch="nvidia", total_vram_gb=24.0, status="online"),
        ]
        insts = [
            InstanceSpec(instance_id="i1", engine_instance_id="e1", replica_idx=0, tp=1, rank=0),
            InstanceSpec(instance_id="i2", engine_instance_id="e2", replica_idx=1, tp=2, rank=1),
        ]
        plan = plan_placement(insts, pool, arch="nvidia", gpu_selector=GpuSelector(gpu_uuids=["c0", "c1", "c2"]))
        by_inst = {p.instance.instance_id: (p.node_id, [g.uuid for g in p.cards]) for p in plan}
        assert by_inst["i2"] == ("n2", ["c1", "c2"])  # 大份先配，占 n2 两张
        assert by_inst["i1"] == ("n1", ["c0"])  # 小份落 n1，选集全消费

    async def test_assign_multi_node_one_node_capacity_raises(self):
        """仅一个节点有足够卡 → 跨机调度失败（需 nnodes 个不同节点）+ ledger 已释放。"""
        pool = [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n1", index=1, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g2", node_id="n2", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
        ]
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        insts = [
            InstanceSpec(instance_id="i0", tp=4, nnodes=2, node_rank=0, replica_idx=0),
            InstanceSpec(instance_id="i1", tp=4, nnodes=2, node_rank=1, replica_idx=0),
        ]
        with pytest.raises(LLMPodValidationError):
            await sched.assign("dep1", insts, pool, vram_per_gpu_gb=8.0, arch="nvidia")
        assert not ledger.get_reserved_for_deployment("dep1")  # 失败回滚（含已 reserve 的 n1 卡）

    async def test_multi_replica_single_node_may_share_node(self):
        """多 replica 单节点：不同 replica 可落同一节点（exclude 仅限跨机同 replica）。"""
        ledger = ResourceLedger()
        sched = GpuScheduler(ledger)
        insts = [
            InstanceSpec(instance_id="i0", tp=1, nnodes=1, replica_idx=0),
            InstanceSpec(instance_id="i1", tp=1, nnodes=1, replica_idx=1),
        ]
        pool = [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=24.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n1", index=1, arch="nvidia", total_vram_gb=24.0, status="online"),
        ]
        await sched.assign("dep1", insts, pool, vram_per_gpu_gb=8.0, arch="nvidia")
        assert insts[0].assigned_node == insts[1].assigned_node == "n1"
        assert insts[0].assigned_gpu_uuids == ["g0"] and insts[1].assigned_gpu_uuids == ["g1"]


class TestRankResolver:
    def test_single_node_defaults(self):
        insts = [InstanceSpec(node_rank=0, nnodes=1, assigned_node="n1")]
        RankResolver.fill(insts, default_master_port=29501, get_node_ip=lambda nid: "10.0.1.5")
        assert insts[0].master_port == 29501
        assert insts[0].master_addr == "10.0.1.5"  # rank-0 节点 IP

    def test_no_resolver_fn(self):
        insts = [InstanceSpec(nnodes=1, assigned_node="n1")]
        RankResolver.fill(insts, default_master_port=29501)  # 无 get_node_ip
        assert insts[0].master_addr == "127.0.0.1"
        assert insts[0].node_rank == 0

    def test_multi_node_master_from_rank0(self):
        insts = [
            InstanceSpec(node_rank=0, nnodes=2, assigned_node="n1"),
            InstanceSpec(node_rank=1, nnodes=2, assigned_node="n2"),
        ]
        RankResolver.fill(
            insts, default_master_port=29501, get_node_ip=lambda nid: {"n1": "10.0.1.5", "n2": "10.0.1.6"}[nid]
        )
        # 两 instance 的 master_addr 都指 rank-0 节点
        assert all(i.master_addr == "10.0.1.5" for i in insts)

    def test_multi_replica_master_grouped(self):
        """多 replica 各自独立 master（各组 head 节点 IP）+ master_port 按 replica_idx 去冲突。"""
        insts = [
            InstanceSpec(node_rank=0, nnodes=2, replica_idx=0, assigned_node="n1"),
            InstanceSpec(node_rank=1, nnodes=2, replica_idx=0, assigned_node="n2"),
            InstanceSpec(node_rank=0, nnodes=2, replica_idx=1, assigned_node="n3"),
            InstanceSpec(node_rank=1, nnodes=2, replica_idx=1, assigned_node="n4"),
        ]
        ips = {"n1": "10.0.1.1", "n2": "10.0.1.2", "n3": "10.0.1.3", "n4": "10.0.1.4"}
        RankResolver.fill(insts, default_master_port=29501, get_node_ip=lambda nid: ips[nid])
        assert insts[0].master_addr == insts[1].master_addr == "10.0.1.1"
        assert insts[2].master_addr == insts[3].master_addr == "10.0.1.3"
        assert insts[0].master_port == insts[1].master_port == 29501
        assert insts[2].master_port == insts[3].master_port == 29502


def _gpu(uuid, node_id, index, arch="nvidia", status="online", avail=80.0, total=80.0):
    """造 ClusterGpuInfo（inspect 推荐选卡的真实入参类型）。"""
    return ClusterGpuInfo(
        uuid=uuid,
        node_id=node_id,
        index=index,
        arch=arch,
        status=status,
        total_vram_gb=total,
        available_vram_gb=avail,
    )


def _insts(tp, nnodes=1):
    """造 expand_topology 等价的 InstanceSpec 列表（单 replica）。"""
    if nnodes <= 1:
        return [InstanceSpec(tp=tp, nnodes=1)]
    return [InstanceSpec(tp=tp, nnodes=nnodes, node_rank=r, replica_idx=0) for r in range(nnodes)]


class TestRecommendPlacement:
    """inspect 推荐选卡——与 GpuScheduler.assign 共用 plan_placement 内核（仅 card_ok 数据源不同）。"""

    def test_single_node_picks_roomy_node(self):
        pool = [_gpu("g0", "n1", 0), _gpu("g1", "n1", 1), _gpu("g2", "n2", 0)]
        uuids, nodes = recommend_placement(_insts(2), pool, arch="nvidia", required_vram_gb=14.0)
        assert uuids == ["g0", "g1"] and nodes == ["n1"]

    def test_binpack_prefers_tightest_fitting_node(self):
        # n1 有 2 张、n2 有 3 张，need=2 都装得下 → binpack 选更紧的 n1（省出整节点 n2）
        pool = [
            _gpu("g0", "n1", 0),
            _gpu("g1", "n1", 1),
            _gpu("g2", "n2", 0),
            _gpu("g3", "n2", 1),
            _gpu("g4", "n2", 2),
        ]
        uuids, nodes = recommend_placement(_insts(2), pool, arch="nvidia", required_vram_gb=14.0)
        assert nodes == ["n1"] and uuids == ["g0", "g1"]

    def test_cross_node_spans_nnodes(self):
        pool = [
            _gpu("g0", "n1", 0),
            _gpu("g1", "n1", 1),
            _gpu("g2", "n2", 0),
            _gpu("g3", "n2", 1),
            _gpu("g4", "n3", 0),
        ]
        uuids, nodes = recommend_placement(_insts(2, nnodes=2), pool, arch="nvidia", required_vram_gb=7.0)
        # binpack 组级：{n1,n3}（容量 2+1）比 {n1,n2}（2+2）用得更紧，保住 n2 整对
        assert nodes == ["n1", "n3"] and len(uuids) == 2

    def test_arch_mismatch_excluded(self):
        pool = [_gpu("g0", "n1", 0, arch="cann")]
        uuids, nodes = recommend_placement(_insts(1), pool, arch="nvidia", required_vram_gb=7.0)
        assert uuids == [] and nodes == []

    def test_offline_excluded(self):
        pool = [_gpu("g0", "n1", 0, status="offline")]
        uuids, nodes = recommend_placement(_insts(1), pool, arch="nvidia", required_vram_gb=7.0)
        assert uuids == [] and nodes == []

    def test_vram_headroom_filter(self):
        # per_card = 14/(1×1) = 14；available×0.9 = 9 < 14 → 不够
        pool = [_gpu("g0", "n1", 0, avail=10.0)]
        uuids, _ = recommend_placement(_insts(1), pool, arch="nvidia", required_vram_gb=14.0)
        assert uuids == []

    def test_unknown_size_skips_vram_check(self):
        pool = [_gpu("g0", "n1", 0, avail=1.0)]  # avail 极小但 size 未知 → 乐观放行
        uuids, nodes = recommend_placement(_insts(1), pool, arch="nvidia", required_vram_gb=None)
        assert uuids == ["g0"] and nodes == ["n1"]

    def test_insufficient_count_returns_empty(self):
        pool = [_gpu("g0", "n1", 0)]  # 仅 1 张，tp=2 不够
        uuids, nodes = recommend_placement(_insts(2), pool, arch="nvidia", required_vram_gb=7.0)
        assert uuids == [] and nodes == []

    def test_empty_instances_returns_empty(self):
        pool = [_gpu("g0", "n1", 0)]
        assert recommend_placement([], pool, arch="nvidia", required_vram_gb=7.0) == ([], [])


class TestPlacementConsistency:
    """契约：recommend_placement（前端展示）与 GpuScheduler.assign（部署分配）走同一选卡内核。

    同一拓扑 + 同一池子（GpuCandidate.total = ClusterGpuInfo.available，使两者 VRAM 判定对齐）下，
    assign 实际分配的卡集 == recommend 推荐的卡集。锁住「controller 推荐 = 前端展示」一致性。
    """

    async def test_recommend_matches_assign_single_node(self):
        # n1 两张、n2 两张；tp=2 → 两边都应选 n1 的 g0,g1（eligible 相同、node_id 升序取 n1）
        cand = [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=80.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n1", index=1, arch="nvidia", total_vram_gb=80.0, status="online"),
            GpuCandidate(uuid="g2", node_id="n2", index=0, arch="nvidia", total_vram_gb=80.0, status="online"),
            GpuCandidate(uuid="g3", node_id="n2", index=1, arch="nvidia", total_vram_gb=80.0, status="online"),
        ]
        cinfo = [_gpu(c.uuid, c.node_id, c.index, avail=80.0) for c in cand]
        ledger = ResourceLedger()
        result = await GpuScheduler(ledger).assign(
            "dep", [InstanceSpec(instance_id="i0", tp=2)], cand, vram_per_gpu_gb=7.0, arch="nvidia"
        )
        rec_uuids, rec_nodes = recommend_placement(_insts(2), cinfo, arch="nvidia", required_vram_gb=14.0)
        assert set(rec_uuids) == set(result[0].gpu_uuids)
        assert rec_nodes == [result[0].node_id]

    async def test_recommend_matches_assign_cross_node(self):
        cand = [
            GpuCandidate(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=80.0, status="online"),
            GpuCandidate(uuid="g1", node_id="n1", index=1, arch="nvidia", total_vram_gb=80.0, status="online"),
            GpuCandidate(uuid="g2", node_id="n2", index=0, arch="nvidia", total_vram_gb=80.0, status="online"),
            GpuCandidate(uuid="g3", node_id="n2", index=1, arch="nvidia", total_vram_gb=80.0, status="online"),
        ]
        cinfo = [_gpu(c.uuid, c.node_id, c.index, avail=80.0) for c in cand]
        insts = [
            InstanceSpec(instance_id="i0", tp=4, nnodes=2, node_rank=0, replica_idx=0),
            InstanceSpec(instance_id="i1", tp=4, nnodes=2, node_rank=1, replica_idx=0),
        ]
        ledger = ResourceLedger()
        result = await GpuScheduler(ledger).assign("dep", insts, cand, vram_per_gpu_gb=3.5, arch="nvidia")
        rec_uuids, rec_nodes = recommend_placement(_insts(4, nnodes=2), cinfo, arch="nvidia", required_vram_gb=14.0)
        assert set(rec_uuids) == {uuid for a in result for uuid in a.gpu_uuids}
        assert set(rec_nodes) == {a.node_id for a in result}


class TestCardFitsPhysicalGate:
    """容量过滤的物理可用闸：卡上已有占用（含平台外进程）时不再被推荐/调度。

    账本看不见平台外进程的占用，只查账本会把副本落到快满的卡上、容器起来即 OOM
    （实测：外部容器占掉 18.47GB/22GB 的卡，账本仍报「整卡空闲」）。
    """

    def _cand(self, **kw):
        defaults = dict(uuid="g0", node_id="n1", index=0, arch="nvidia", total_vram_gb=22.0, status="online")
        defaults.update(kw)
        return GpuCandidate(**defaults)

    def test_nearly_full_card_rejected(self):
        sched = GpuScheduler(ResourceLedger())
        assert not sched.card_fits(self._cand(used_vram_gb=18.47), 9.26)

    def test_empty_card_accepted(self):
        sched = GpuScheduler(ResourceLedger())
        assert sched.card_fits(self._cand(used_vram_gb=0.52), 9.26)

    def test_missing_heartbeat_is_optimistic(self):
        """无实时数据（used_vram_gb=None）按整卡空闲——与账本口径一致，不误拒。"""
        sched = GpuScheduler(ResourceLedger())
        assert sched.card_fits(self._cand(), 9.26)

    def test_driver_baseline_does_not_kill_whole_card(self):
        """本底占用不折二次上限：Ascend 常驻 1.81GB，21.02×0.9=18.9 ≤ 19.21 仍可用。

        物理闸不再乘利用率上限（reserve 已含上限）——否则带本底占用的卡永远排不出去。
        """
        sched = GpuScheduler(ResourceLedger())
        assert sched.card_fits(self._cand(uuid="asc", arch="cann", total_vram_gb=21.02, used_vram_gb=1.81), 9.26)

    async def test_assign_skips_nearly_full_card(self):
        """调度也不选快满的卡：n1 满、n2 空 → 落 n2（同节点内也一样绕开）。"""
        ledger = ResourceLedger()
        pool = [
            GpuCandidate(
                uuid="full",
                node_id="n1",
                index=0,
                arch="nvidia",
                total_vram_gb=22.0,
                status="online",
                used_vram_gb=18.47,
            ),
            GpuCandidate(
                uuid="free", node_id="n2", index=0, arch="nvidia", total_vram_gb=22.0, status="online", used_vram_gb=0.5
            ),
        ]
        insts = [InstanceSpec(instance_id="i1", tp=1)]
        await GpuScheduler(ledger).assign("dep1", insts, pool, vram_per_gpu_gb=9.26, arch="nvidia")
        assert insts[0].assigned_gpu_uuids == ["free"]

    async def test_assign_force_overrides_physical_gate(self):
        """force=true 是唯一逃生口：显存再满也强制调度（超额由引擎/NCCL 兜底）。"""
        ledger = ResourceLedger()
        pool = [
            GpuCandidate(
                uuid="full",
                node_id="n1",
                index=0,
                arch="nvidia",
                total_vram_gb=22.0,
                status="online",
                used_vram_gb=18.47,
            ),
        ]
        insts = [InstanceSpec(instance_id="i1", tp=1)]
        await GpuScheduler(ledger).assign("dep1", insts, pool, vram_per_gpu_gb=9.26, arch="nvidia", force=True)
        assert insts[0].assigned_gpu_uuids == ["full"]


class TestRecommendPredicateMatchesAssign:
    """预览与调度同口径：容量谓词都按「本次预留」判，不按「模型每卡需求 × 系数」。

    历史缺陷（code-review 发现）：recommend_plan 用 ``vram_fits(available, need)``
    （= 可用×0.9 ≥ 需求），assign 用 ``card_fits``（= 可用 ≥ util×total 且 ≥ 需求）。
    一张 22GB 卡有 3GB 外部占用时前者判「够」（19×0.9 ≥ 9.26）、后者判「满」
    （19 < 19.8）→ 预览给出调度结果、创建报无可用卡。
    """

    @staticmethod
    def _rec_uuids(pool, *, required: float, util: float) -> list[str]:
        """跑一遍推荐内核并拍平成 uuid 列表（生产路径同样直接用 recommend_plan）。"""
        from llmpod.apps.manager.deployment.scheduler.gpu import recommend_plan

        plan = recommend_plan(_insts(1), pool, arch="nvidia", required_vram_gb=required, vram_utilization=util)
        return [g.uuid for _i, _n, cards in (plan or []) for g in cards]

    def _pool(self, avail: float, total: float = 22.0):
        return [
            ClusterGpuInfo(
                uuid="g0",
                node_id="n1",
                index=0,
                arch="nvidia",
                status="online",
                total_vram_gb=total,
                available_vram_gb=avail,
            )
        ]

    def test_card_with_foreign_usage_is_not_recommended(self):
        uuids = self._rec_uuids(self._pool(avail=19.0), required=9.26, util=0.9)
        assert uuids == []  # 19GB 留不出整卡预留 19.8GB

    async def test_preview_and_assign_agree_on_foreign_usage(self):
        """同一张「外部占用 3GB」的卡：推荐为空 ⇔ assign 也选不出（两边一致）。"""
        avail = 19.0
        assert self._rec_uuids(self._pool(avail), required=9.26, util=0.9) == []

        cand = [
            GpuCandidate(
                uuid="g0",
                node_id="n1",
                index=0,
                arch="nvidia",
                total_vram_gb=22.0,
                status="online",
                used_vram_gb=22.0 - avail,
            )
        ]
        with pytest.raises(LLMPodValidationError):
            await GpuScheduler(ResourceLedger()).assign(
                "dep1", [InstanceSpec(instance_id="i1", tp=1)], cand, vram_per_gpu_gb=9.26, arch="nvidia"
            )

    def test_roomy_card_still_recommended(self):
        assert self._rec_uuids(self._pool(avail=21.48), required=9.26, util=0.9) == ["g0"]

    def test_lowered_utilization_allows_shared_card(self):
        """调低生效占比（共享同卡）：预留变小 → 同一张卡重新可选（与 card_fits 同口径）。"""
        assert self._rec_uuids(self._pool(avail=19.0), required=9.26, util=0.45) == ["g0"]
