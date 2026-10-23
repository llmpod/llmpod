"""资源测算单测：KV 真公式/启发式、逐层 offload 表、三约束、场景枚举、部署形态求解。

纯函数无 DB；数值口径以 `deployment/resource.py` 为唯一定义处。
"""

from __future__ import annotations

from types import SimpleNamespace

from llmpod.apps.manager.deployment import resource as R


def _model(num_layers=None, num_kv_heads=None, head_dim=None):
    return SimpleNamespace(num_layers=num_layers, num_kv_heads=num_kv_heads, head_dim=head_dim)


def _cand(node_id, vram, ram):
    return R.LaneCandidate(node_id=node_id, vram_avail_gb=vram, ram_avail_gb=ram)


# ============ KV 占用（唯一计算入口） ============


class TestKVCacheGb:
    def test_no_kv_cache_or_ctx_is_zero(self):
        assert R.kv_cache_gb(10.0, None) == 0.0
        assert R.kv_cache_gb(10.0, 8192, has_kv_cache=False) == 0.0
        assert R.kv_cache_gb(None, 8192) == 0.0

    def test_heuristic_scales_with_ctx_and_dtype(self):
        # 启发式基数 = W×0.1 @8192；精度档按字节数线性折算
        assert R.kv_cache_gb(10.0, 8192) == 1.0
        assert R.kv_cache_gb(10.0, 32768) == 4.0
        assert R.kv_cache_gb(10.0, 8192, dtype_bytes=1.0) == 0.5
        assert R.kv_cache_gb(10.0, 8192, dtype_bytes=0.5) == 0.25

    def test_formula_uses_arch_meta(self):
        # 2 × n_seq(4) × 8192 × 32 层 × 8 KV head × 128 dim × 1B = 2 GiB
        meta = R.KVArchMeta(num_layers=32, num_kv_heads=8, head_dim=128)
        assert R.kv_cache_gb(10.0, 8192, dtype_bytes=1.0, arch_meta=meta) == 2.0
        # f16 翻倍
        assert R.kv_cache_gb(10.0, 8192, dtype_bytes=2.0, arch_meta=meta) == 4.0
        # 并发序列数可覆盖
        assert R.kv_cache_gb(10.0, 8192, dtype_bytes=1.0, arch_meta=meta, n_parallel=2) == 1.0

    def test_formula_ignores_weight(self):
        """真公式只由架构参数与上下文决定，与权重规模无关。"""
        meta = R.KVArchMeta(num_layers=32, num_kv_heads=8, head_dim=128)
        assert R.kv_cache_gb(100.0, 8192, dtype_bytes=1.0, arch_meta=meta) == 2.0


class TestKVArchMeta:
    def test_from_model_requires_all_positive(self):
        assert R.KVArchMeta.from_model(_model(32, 8, 128)) == R.KVArchMeta(32, 8, 128)
        assert R.KVArchMeta.from_model(_model(32, 8, None)) is None
        assert R.KVArchMeta.from_model(_model(32, 0, 128)) is None
        assert R.KVArchMeta.from_model(_model()) is None
        assert R.KVArchMeta.from_model(SimpleNamespace()) is None


# ============ 逐层表 + 三约束 ============


class TestEstimateMemoryRequirements:
    BASE = dict(weight_gb=100.0, kv_gb=10.0, total_layers=10)

    def _est(self, **kw):
        return R.estimate_memory_requirements(vram_avail_gb=200.0, ram_avail_gb=100.0, **{**self.BASE, **kw})

    def test_unknown_inputs_returns_none(self):
        assert (
            R.estimate_memory_requirements(
                weight_gb=None, kv_gb=10.0, total_layers=10, vram_avail_gb=200.0, ram_avail_gb=100.0
            )
            is None
        )
        assert (
            R.estimate_memory_requirements(
                weight_gb=100.0, kv_gb=10.0, total_layers=None, vram_avail_gb=200.0, ram_avail_gb=100.0
            )
            is None
        )

    def test_tiers_cover_every_layer(self):
        est = self._est()
        assert [t.offload_layers for t in est.tiers] == list(range(11))

    def test_tier_requirements_are_monotone(self):
        est = self._est()
        vram = [t.vram_gb for t in est.tiers]
        ram = [t.ram_gb for t in est.tiers]
        assert vram == sorted(vram)  # 层数越多显存越高
        assert ram == sorted(ram, reverse=True)  # 层数越多内存越低

    def test_kv_follows_weights_across_pools(self):
        """KV 落点不可配：n>0 恒随权重上卡，n=0（纯 CPU）恒落内存——各只进一边。"""
        est = self._est()
        # n=0：权重 + KV 全在内存侧
        assert est.tiers[0].vram_gb == 0.0
        assert est.tiers[0].ram_gb == 110.0
        # n=10（全量上卡）：权重 + KV 全在显存侧
        assert est.tiers[10].vram_gb == 110.0
        assert est.tiers[10].ram_gb == 0.0
        # 中间档：权重按比例分池，KV 恒在显存侧（不出现「KV 落内存」的档）
        assert est.tiers[5].ram_gb == 50.0
        assert est.tiers[5].vram_gb == 60.0

    def test_full_gpu_tier_when_both_pools_fit(self):
        est = self._est()
        assert est.scenario == R.SCENARIO_GPU_ONLY
        assert est.recommended_tier == 10

    def test_no_solution_when_vram_short(self):
        # 200×0.9=180 < 110? 不，110 ≤ 180 够。改用 100 显存：cap 90，tier10 需 110 → 无解
        est = R.estimate_memory_requirements(
            weight_gb=100.0, kv_gb=10.0, total_layers=10, vram_avail_gb=100.0, ram_avail_gb=100.0
        )
        assert est is None

    def test_middle_tiers_never_selected(self):
        """显存 90 cap 装不下满 offload，但 8 层档可行——中间档只进逐层表，永不取解。"""
        kw = dict(weight_gb=100.0, kv_gb=10.0, total_layers=10, vram_avail_gb=100.0, ram_avail_gb=100.0)
        assert R.estimate_memory_requirements(**kw) is None
        # 两端都不可行（满 offload 装不下、纯 CPU 内存不够）→ 无解，不取中间档
        assert R.estimate_memory_requirements(**kw, allow_cpu=True) is None
        assert R.estimate_memory_requirements(**kw, allow_cpu=True, preference=R.SCENARIO_GPU_MAIN) is None
        # 逐层表仍列中间档（前端资源面板展示用）
        est = R.estimate_memory_requirements(
            weight_gb=100.0,
            kv_gb=10.0,
            total_layers=10,
            vram_avail_gb=200.0,
            ram_avail_gb=200.0,
            allow_cpu=True,
        )
        assert est.tiers[8].feasible is True and est.tiers[8].offload_layers == 8

    def test_cpu_only_puts_kv_in_ram(self):
        """n=0 没有卡：KV 只能落内存（llama.cpp -ngl 0 时 KV 本来就在主机内存）。"""
        kw = dict(weight_gb=100.0, kv_gb=10.0, total_layers=10, vram_avail_gb=0.0, ram_avail_gb=200.0)
        est = R.estimate_memory_requirements(**kw, allow_cpu=True)
        assert est is not None
        assert est.scenario == R.SCENARIO_CPU_ONLY
        assert est.recommended_tier == 0
        assert est.tiers[0].ram_gb == 110.0 and est.tiers[0].vram_gb == 0.0

    def test_total_weight_constraint(self):
        """权重超过两池之和 → 全档不可行（约束①）。"""
        est = R.estimate_memory_requirements(
            weight_gb=300.0, kv_gb=10.0, total_layers=10, vram_avail_gb=100.0, ram_avail_gb=100.0, allow_cpu=True
        )
        assert est is None

    def test_preference_picks_end(self):
        """两端都可行时，偏好决定取哪一端（不再有中间档取点）。"""
        kw = dict(weight_gb=100.0, kv_gb=10.0, total_layers=10, vram_avail_gb=200.0, ram_avail_gb=200.0, allow_cpu=True)
        assert R.estimate_memory_requirements(**kw, preference=R.SCENARIO_CPU_ONLY).recommended_tier == 0
        assert R.estimate_memory_requirements(**kw, preference=R.SCENARIO_CPU_MAIN).recommended_tier == 0
        assert R.estimate_memory_requirements(**kw, preference=R.SCENARIO_GPU_MAIN).recommended_tier == 10
        assert R.estimate_memory_requirements(**kw, preference=R.SCENARIO_GPU_ONLY).recommended_tier == 10
        # 不可用的偏好回退 auto（全量上卡优先）
        assert R.estimate_memory_requirements(**kw, preference="bogus").recommended_tier == 10

    def test_kv_source_is_reported(self):
        assert self._est().kv_source == "formula"
        assert self._est(kv_source="heuristic").kv_source == "heuristic"


# ============ 部署形态求解 / 节点排序 ============
class TestPlanLane:
    KW = dict(weight_gb=100.0, kv_gb=10.0, total_layers=10)

    def test_picks_gpu_only_when_available(self):
        plan = R.plan_lane(candidates=[_cand("n1", 200.0, 100.0)], **self.KW)
        assert plan is not None
        assert plan.scenario == R.SCENARIO_GPU_ONLY
        assert plan.offload_layers == 10
        assert plan.node_id == "n1"
        assert plan.vram_gb == 110.0 and plan.ram_gb == 0.0

    def test_hybrid_never_selected_even_when_requested(self):
        """显式 gpu_main/cpu_main 也不取中间档：装不下整模型就退纯 CPU（或全量上卡）。"""
        cands = [_cand("gpu-big", 100.0, 0.0), _cand("cpu-rich", 100.0, 400.0)]
        plan = R.plan_lane(candidates=cands, allow_cpu=True, preference=R.SCENARIO_GPU_MAIN, **self.KW)
        assert plan is not None
        assert plan.node_id == "cpu-rich"
        assert plan.scenario == R.SCENARIO_CPU_ONLY  # 不落混合档
        assert plan.offload_layers == 0

        # auto 同样两端取解：gpu-big 无内存不可行、cpu-rich 满 offload 装不下 → 纯 CPU
        auto_plan = R.plan_lane(candidates=cands, allow_cpu=True, **self.KW)
        assert auto_plan is not None
        assert auto_plan.scenario == R.SCENARIO_CPU_ONLY

    def test_cpu_only_does_not_fall_back_to_gpu(self):
        """显式 cpu_only 且内存放不下 → 无解（不静默回退到显存档）。"""
        cand = _cand("n1", 200.0, 50.0)
        assert R.plan_lane(candidates=[cand], allow_cpu=True, preference=R.SCENARIO_CPU_ONLY, **self.KW) is None
        auto_plan = R.plan_lane(candidates=[cand], allow_cpu=True, **self.KW)
        assert auto_plan is not None and auto_plan.scenario == R.SCENARIO_GPU_ONLY

    def test_gpu_only_does_not_fall_back_to_hybrid_or_cpu(self):
        """显式 gpu_only 且全量上卡不可行 → 无解（不静默降到混合档/纯 CPU）。"""
        cand = _cand("n1", 100.0, 400.0)  # 显存差一点、内存充裕
        assert R.plan_lane(candidates=[cand], allow_cpu=True, preference=R.SCENARIO_GPU_ONLY, **self.KW) is None
        # 同一节点在 auto 下解成纯 CPU——用户显式要「仅 GPU」时不得如此
        auto_plan = R.plan_lane(candidates=[cand], allow_cpu=True, **self.KW)
        assert auto_plan is not None and auto_plan.scenario == R.SCENARIO_CPU_ONLY

    def test_pool_is_taken_as_is(self):
        """池按原值判（110 对池 110 可行）：折让只在池的装配处折一次，这里不再折。"""
        assert R.plan_lane(candidates=[_cand("n1", 110.0, 0.0)], **self.KW) is not None
        assert R.plan_lane(candidates=[_cand("n1", 109.0, 0.0)], **self.KW) is None

    def test_none_when_no_candidate_fits(self):
        assert R.plan_lane(candidates=[_cand("n1", 10.0, 10.0)], **self.KW) is None
        assert R.plan_lane(candidates=[], **self.KW) is None

    def test_prefers_larger_pool_within_same_scenario(self):
        plan = R.plan_lane(candidates=[_cand("small", 120.0, 120.0), _cand("big", 400.0, 400.0)], **self.KW)
        assert plan is not None and plan.node_id == "big"

    def test_cpu_only_preference_beats_available_vram(self):
        """显式 cpu_only：节点有充裕显存也走纯 CPU（「点节点卡片 = CPU 部署」依赖此口径）。"""
        cand = _cand("n1", 200.0, 200.0)
        plan = R.plan_lane(
            candidates=[cand],
            allow_cpu=True,
            preference=R.SCENARIO_CPU_ONLY,
            **self.KW,
        )
        assert plan is not None
        assert plan.scenario == R.SCENARIO_CPU_ONLY
        assert plan.offload_layers == 0
        # 同一节点在 auto 下解成显存满上——前端必须显式传 cpu_only，否则部署形态不符
        auto_plan = R.plan_lane(candidates=[cand], allow_cpu=True, **self.KW)
        assert auto_plan is not None and auto_plan.scenario == R.SCENARIO_GPU_ONLY


class TestLaneCandidates:
    """候选池装配：两池都取账本可用量（折让在那里折过一次，下游不再折）。"""

    class _Ledger:
        def __init__(self, vram: dict, ram: dict):
            self._vram, self._ram = vram, ram

        def get_available(self, gpu_uuid: str, total_gb: float) -> float:
            return self._vram.get(gpu_uuid, 0.0)

        def get_available_ram(self, node_id: str, total_gb: float) -> float:
            return self._ram.get(node_id, 0.0)

    @staticmethod
    def _gpu(uuid, node_id, arch, total=24.0):
        return SimpleNamespace(uuid=uuid, node_id=node_id, arch=arch, total_vram_gb=total)

    def test_pools_taken_from_ledger(self):
        """显存取该节点**引擎可用架构**卡的单卡最大可用（跨卡不累加），内存取节点账本量。"""
        gpus = [
            self._gpu("g1", "n1", "nvidia"),
            self._gpu("g2", "n1", "nvidia"),
            self._gpu("c1", "n1", "cann", total=40.0),
        ]
        ledger = self._Ledger({"g1": 10.0, "g2": 22.0, "c1": 36.0}, {"n1": 50.0})
        nodes = [SimpleNamespace(node_id="n1", memory_gb=64.0)]

        cands = R.lane_candidates(nodes, gpus, ledger, gpu_archs=["nvidia"])
        assert cands == [R.LaneCandidate(node_id="n1", vram_avail_gb=22.0, ram_avail_gb=50.0)]

    def test_no_arch_filter_counts_every_card(self):
        gpus = [self._gpu("c1", "n1", "cann", total=40.0)]
        ledger = self._Ledger({"c1": 36.0}, {"n1": 50.0})
        nodes = [SimpleNamespace(node_id="n1", memory_gb=64.0)]
        assert R.lane_candidates(nodes, gpus, ledger)[0].vram_avail_gb == 36.0

    def test_heartbeat_used_caps_ledger_availability(self):
        """回归：卡被外部进程占着（账本看不见）时，求解池取账本与心跳物理可用的较小值。

        只看账本会解出 GPU 形态，调度的物理闸（physical_fits）逐卡判「显存不足」直接
        失败——预检（读心跳）判 CPU 可行的请求变成「预检绿、创建必败」。
        """
        gpus = [self._gpu("g1", "n1", "nvidia", total=80.0)]
        gpus[0].used_vram_gb = 60.0  # 心跳：外部进程已占 60GB → 物理可用 20
        ledger = self._Ledger({"g1": 72.0}, {"n1": 50.0})  # 账本（无平台预留）说 72
        nodes = [SimpleNamespace(node_id="n1", memory_gb=64.0)]
        cand = R.lane_candidates(nodes, gpus, ledger)[0]
        assert cand.vram_avail_gb == 20.0  # min(账本 72, 物理 80−60)

    def test_heartbeat_missing_keeps_ledger_only(self):
        """心跳未上报（used_vram_gb=None）→ 乐观纯账本口径（与物理闸跳过语义一致）。"""
        gpus = [self._gpu("g1", "n1", "nvidia", total=80.0)]  # 无 used_vram_gb 属性值
        ledger = self._Ledger({"g1": 72.0}, {"n1": 50.0})
        nodes = [SimpleNamespace(node_id="n1", memory_gb=64.0)]
        assert R.lane_candidates(nodes, gpus, ledger)[0].vram_avail_gb == 72.0

    def test_pinned_node_missing_yields_empty_pool(self):
        """指定的节点不在候选里 → 空池（不静默放宽为全部节点，怎么报由调用方定）。"""
        ledger = self._Ledger({}, {"n1": 50.0, "n2": 50.0})
        nodes = [SimpleNamespace(node_id="n1", memory_gb=64.0), SimpleNamespace(node_id="n2", memory_gb=64.0)]
        assert R.lane_candidates(nodes, [], ledger, pinned="n-gone") == []
        assert [c.node_id for c in R.lane_candidates(nodes, [], ledger, pinned="n2")] == ["n2"]

    def test_node_without_cards_still_candidate(self):
        """无卡节点也进池（vram 0 → 只能纯 CPU）：纯 CPU 部署的候选不能因无卡被漏掉。"""
        ledger = self._Ledger({}, {"n1": 50.0})
        nodes = [SimpleNamespace(node_id="n1", memory_gb=64.0)]
        assert R.lane_candidates(nodes, [], ledger) == [
            R.LaneCandidate(node_id="n1", vram_avail_gb=0.0, ram_avail_gb=50.0)
        ]


class TestForcedCpuLane:
    """强制部署兜底档：正常求解无解时仍给调度结果（超额记账，容量交引擎兜底）。"""

    KW = dict(weight_gb=100.0, kv_gb=10.0, total_layers=10)

    def test_overcommits_on_roomiest_node(self):
        plan = R.forced_cpu_lane(candidates=[_cand("small", 0.0, 50.0), _cand("big", 0.0, 80.0)], **self.KW)
        assert plan.scenario == R.SCENARIO_CPU_ONLY
        assert plan.offload_layers == 0
        assert plan.node_id == "big"  # 可用内存最大者
        # 需求照算（权重 + KV），不因强制而缩水
        assert plan.ram_gb == 110.0 and plan.vram_gb == 0.0
        assert plan.memory.scenario == R.SCENARIO_CPU_ONLY
        assert plan.memory.tiers[0].feasible is False  # 该档确实不可行，只是被强制调度

    def test_tier_math_puts_kv_with_weights(self):
        """n>0 时 KV 只进显存侧（纯 CPU 档才进内存）——逐层表按 n 直接验算。"""
        est = R.estimate_memory_requirements(
            weight_gb=100.0,
            kv_gb=10.0,
            total_layers=10,
            vram_avail_gb=200.0,
            ram_avail_gb=200.0,
            allow_cpu=True,
        )
        tier = est.tiers[1]  # 1 层档：0.9×100 内存 + (0.1×100 + 10) 显存
        assert tier.ram_gb == 90.0
        assert tier.vram_gb == 20.0
        assert est.tiers[0].ram_gb == 110.0  # 纯 CPU 档 KV 落内存

    def test_unknown_shape_still_places(self):
        plan = R.forced_cpu_lane(weight_gb=None, kv_gb=0.0, total_layers=None, candidates=[_cand("n1", 0.0, 10.0)])
        assert plan.node_id == "n1" and plan.ram_gb == 0.0

    def test_requires_candidate(self):
        import pytest

        with pytest.raises(ValueError):
            R.forced_cpu_lane(candidates=[], **self.KW)
