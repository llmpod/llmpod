"""recommend.py 纯函数单测：引擎推荐 / 配置推荐 / tp 推导 / 集群选择。

零 DB 依赖——全部用 dataclass / mock 对象直接喂入。
"""

from __future__ import annotations

from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.parallel import ParallelShape
from llmpod.apps.manager.deployment.recommend import (
    GpuPoolEntry,
    build_recommendation,
    derive_tp,
    feasible_tp_for_node,
    pick_cluster,
    recommend_engine,
)


class TestRecommendEngine:
    def test_llm_matches_vllm(self):
        """llm 类型匹配 vllm（首个 supported_categories 含 llm 的引擎）。"""
        engine = recommend_engine("llm")
        assert engine is not None
        assert "llm" in get_adapter(engine).supported_categories

    def test_embedding_matches(self):
        engine = recommend_engine("embedding")
        assert engine is not None
        assert "embedding" in get_adapter(engine).supported_categories

    def test_unknown_type_returns_none(self):
        assert recommend_engine("nonexistent_type") is None

    def test_none_returns_none(self):
        assert recommend_engine(None) is None

    def test_format_routes_gguf_to_llamacpp(self):
        """gguf 不被 vllm/sglang 支持 → 越过它们落到 llamacpp（修复：原只看 type 会错配 vllm）。"""
        engine = recommend_engine("llm", "gguf")
        assert engine == "llamacpp"
        assert "gguf" in get_adapter(engine).supported_formats

    def test_format_routes_safetensors_to_vllm(self):
        """safetensors + llm → vllm（注册表首个同时满足 type+format 的）。"""
        assert recommend_engine("llm", "safetensors") == "vllm"

    def test_format_excludes_unsupported(self):
        """gguf + rerank → 无引擎支持（llamacpp 不支持 rerank）→ None。"""
        assert recommend_engine("rerank", "gguf") is None

    def test_unknown_format_falls_back_to_type_only(self):
        """format 未知（None）→ 退化为只按 type 匹配（不因 format 缺失而误拒）。"""
        engine = recommend_engine("llm", None)
        assert engine == "vllm"


class TestBuildRecommendation:
    def test_returns_adapter_defaults(self):
        adapter = get_adapter("vllm")
        rec = build_recommendation(adapter, model_size_gb=14.0, model_type="llm", engine_name="vllm")
        assert rec.engine == "vllm"
        assert rec.image  # 非空镜像 ref
        assert rec.topology["tp"] == 1
        assert rec.topology["nnodes"] == 1
        assert rec.topology["replicas"] == 1
        assert rec.vram_per_gpu_gb > 0

    def test_zero_size(self):
        """model_size_gb=0 → vram 估算为 0（不崩）。"""
        adapter = get_adapter("vllm")
        rec = build_recommendation(adapter, model_size_gb=0, model_type="llm", engine_name="vllm")
        assert rec.vram_per_gpu_gb >= 0

    def test_context_increases_vram(self):
        """推荐显存含 KV：未传 context 按 KV_BASE_CONTEXT(8192) 兜底；context 越大 vram 越大。"""
        adapter = get_adapter("vllm")
        default_ctx = build_recommendation(
            adapter, model_size_gb=14.0, model_type="llm", engine_name="vllm", context_size=None
        )
        # 14 + 14×0.1×(8192/8192) = 15.4
        assert default_ctx.vram_per_gpu_gb == 15.4
        big_ctx = build_recommendation(
            adapter, model_size_gb=14.0, model_type="llm", engine_name="vllm", context_size=32768
        )
        assert big_ctx.vram_per_gpu_gb > default_ctx.vram_per_gpu_gb

    def test_max_context_clamps_default(self):
        """模型自带上限低于兜底 8192 → 推荐值钳到上限（超限是引擎硬拒，不是估算误差）。"""
        adapter = get_adapter("vllm")
        rec = build_recommendation(
            adapter, model_size_gb=14.0, model_type="llm", engine_name="vllm", model_max_context=4096
        )
        assert rec.topology["max_model_len"] == 4096
        # 14 + 14×0.1×(4096/8192) = 14.7
        assert rec.vram_per_gpu_gb == 14.7

    def test_max_context_above_default_keeps_default(self):
        """上限高于兜底值时不放大——兜底值只用来算 KV，不是要跑满模型最大上下文。"""
        adapter = get_adapter("vllm")
        rec = build_recommendation(
            adapter, model_size_gb=14.0, model_type="llm", engine_name="vllm", model_max_context=131072
        )
        assert rec.topology["max_model_len"] == 8192

    def test_explicit_context_clamped_by_max_context(self):
        """用户显式要的值同样受上限钳制（免得把硬拒值原样发给引擎）。"""
        adapter = get_adapter("vllm")
        rec = build_recommendation(
            adapter,
            model_size_gb=14.0,
            model_type="llm",
            engine_name="vllm",
            context_size=32768,
            model_max_context=8192,
        )
        assert rec.topology["max_model_len"] == 8192

    def test_nonpositive_max_context_treated_as_unknown(self):
        """0/负数当「未知」而非硬钳制：钳到 0 会让 KV 估算归零、推荐上下文为 0。"""
        adapter = get_adapter("vllm")
        for bogus in (0, -1):
            rec = build_recommendation(
                adapter, model_size_gb=14.0, model_type="llm", engine_name="vllm", model_max_context=bogus
            )
            assert rec.topology["max_model_len"] == 8192


class TestDeriveTp:
    """tp 推导：最小可行拓扑 + 凑不够的组不参与（不拿卡数顶替 need）。"""

    def _card(self, node, vram=22.0, avail=None, arch="nvidia"):
        return GpuPoolEntry(
            uuid=f"{node}-{vram}",
            node_id=node,
            arch=arch,
            total_vram_gb=vram,
            available_vram_gb=avail if avail is not None else vram,
        )

    def test_single_card_enough_returns_1(self):
        """单卡装得下（need=1）→ tp=1（能一张卡跑就不推两张）。"""
        cards = [self._card("n1", vram=80.0)]
        assert derive_tp(cards, required_vram_gb=33.0, arch_order=["nvidia"]) == 1

    def test_two_small_cards_derive_2(self):
        """33GB 模型 + 2×22GB（cap 19.8）→ need=2、组内 2 张 → tp=2。"""
        cards = [self._card("n1"), self._card("n1")]
        assert derive_tp(cards, required_vram_gb=33.0, arch_order=["nvidia"]) == 2

    def test_min_over_heterogeneous_nodes(self):
        """异构集群取最小可行：n1 一张 80GB（need=1）+ n2 两张 22GB（need=2）→ tp=1。

        复刻 review 场景：不能取 max 把所有节点都推到 2 卡——单卡 80GB 可行就不占两张小卡。
        """
        cards = [self._card("n1", vram=80.0), self._card("n2"), self._card("n2")]
        assert derive_tp(cards, required_vram_gb=33.0, arch_order=["nvidia"]) == 1

    def test_infeasible_group_excluded(self):
        """凑不够的组不参与推导：60GB 模型 + 仅 2×22GB（need=4 > 2）→ 无可行组 → 1。

        复刻 review 场景：min(need, len) 会把 need=4 伪装成 tp=2——按 2 卡永远放不下，
        推导应放弃（回 1），交给后续校验报「提高 tp 也无用」的真实原因。
        """
        cards = [self._card("n1"), self._card("n1")]
        assert derive_tp(cards, required_vram_gb=60.0, arch_order=["nvidia"]) == 1

    def test_feasible_group_beats_infeasible(self):
        """同集群有可行组（80GB 单卡）与不可行组（22GB×2，need=4）→ 取可行组 need=1。"""
        cards = [self._card("n1", vram=80.0), self._card("n2"), self._card("n2")]
        assert derive_tp(cards, required_vram_gb=60.0, arch_order=["nvidia"]) == 1

    def test_arch_order_filter(self):
        """arch_order 之外的卡不参与推导（cann 组被排除，即便卡多）。"""
        cards = [
            self._card("n1", arch="cann"),
            self._card("n1", arch="cann"),
        ]
        assert derive_tp(cards, required_vram_gb=33.0, arch_order=["nvidia"]) == 1

    def test_required_none_returns_1(self):
        """required 未知 → 不推导。"""
        cards = [self._card("n1")]
        assert derive_tp(cards, required_vram_gb=None, arch_order=["nvidia"]) == 1

    def test_empty_cards_returns_1(self):
        assert derive_tp([], required_vram_gb=33.0, arch_order=["nvidia"]) == 1

    def test_illegal_tp_falls_back_to_next_legal_size(self):
        """显存算得下 tp=3，但 4 个 head 切不开 3 份 → 退到合法的 tp=4。

        45GB ÷ 19.8 = need 3；heads=kv=4 时合法集 {1,2,4} 里没有 3，取最小的 ≥3 即 4。
        旧口径（纯 ceil）会推出 tp=3，容器起来后在引擎建层期 AssertionError。
        """
        cards = [self._card("n1") for _ in range(4)]
        shape = ParallelShape(num_attention_heads=4, num_kv_heads=4)
        assert derive_tp(cards, required_vram_gb=45.0, arch_order=["nvidia"], shape=shape) == 4
        # 对照：不给形状时仍是旧的 ceil 值（保住向后兼容）
        assert derive_tp(cards, required_vram_gb=45.0, arch_order=["nvidia"]) == 3

    def test_no_legal_tp_gives_up_instead_of_emitting_broken_value(self):
        """组内没有合法 tp 能承担所需卡数 → 放弃推导（不推一个必炸的值）。

        heads=32 / kv=6 时 tp=4 非法（6 % 4 ≠ 0），合法集只有 {1,2}；need=4 无解 → 回 1，
        交给后续校验报「放不下」的真实原因。
        """
        cards = [self._card("n1") for _ in range(4)]
        shape = ParallelShape(num_attention_heads=32, num_kv_heads=6)
        assert derive_tp(cards, required_vram_gb=60.0, arch_order=["nvidia"], shape=shape) == 1

    def test_unknown_shape_behaves_like_legacy(self):
        """形状未探测（全 None）→ 合法集退化为 1..卡数，与旧行为逐值一致。"""
        cards = [self._card("n1"), self._card("n1")]
        shape = ParallelShape()
        assert derive_tp(cards, required_vram_gb=33.0, arch_order=["nvidia"], shape=shape) == 2

    def test_busy_small_card_does_not_ruin_the_node(self):
        """节点内一张被占用的卡不该把节点判死——按可用显存最大的前 tp 张判。

        旧口径取节点内最小可用值当每卡预算（本例 min=2GB），80GB 空卡所在节点被判不可行。
        """
        cards = [self._card("n1", vram=80.0, avail=80.0), self._card("n1", vram=80.0, avail=2.0)]
        assert derive_tp(cards, required_vram_gb=33.0, arch_order=["nvidia"]) == 1


class TestFeasibleTpForNode:
    def _card(self, vram, avail):
        return GpuPoolEntry(
            uuid=f"g{vram}-{avail}", node_id="n1", arch="nvidia", total_vram_gb=vram, available_vram_gb=avail
        )

    def test_picks_top_available_cards(self):
        cards = [self._card(80.0, 80.0), self._card(24.0, 3.0)]
        # tp=1 用 80GB 那张就够（72 >= 33），不必被 3GB 的忙卡拖累
        assert feasible_tp_for_node(cards, required_vram_gb=33.0) == 1

    def test_none_when_not_enough(self):
        cards = [self._card(24.0, 24.0), self._card(24.0, 24.0)]
        assert feasible_tp_for_node(cards, required_vram_gb=200.0) is None

    def test_same_tier_combination_not_killed_by_mixed_top_cards(self):
        """同档判定与调度 _tier_uniform 同口径：分档 round(total,1)、可在任意同档子集取组。

        回归：旧实现只在「前 tp 张」上判精确相等——节点混排 1×48G + 3×24G 且需求 55GB
        时，tp=3 的前 3 张必含 48G 卡 → 判跨档 → 该 tp 被否决 → 推荐返回 None；而调度
        在 3×24G 同档卡上完全放得下（55/3 ≈ 18.3 ≤ 21.6）。新版按档分组找「同档且
        够数」的组合，3×24G 命中 tp=3。
        """
        cards = [
            self._card(48.0, 48.0),
            self._card(24.0, 24.0),
            self._card(24.0, 24.0),
            self._card(24.0, 24.0),
        ]
        assert feasible_tp_for_node(cards, required_vram_gb=55.0) == 3

    def test_float_tail_same_tier_not_split(self):
        """浮点尾差（24.0 vs 24.000000000000004）不得把同档卡拆成两档。"""
        cards = [self._card(24.0, 24.0), self._card(24.000000000000004, 24.0)]
        assert feasible_tp_for_node(cards, required_vram_gb=40.0) == 2

    def test_cross_tier_tp_rejected_when_no_uniform_group(self):
        """凑不出同档组（每档各只有 1 张）→ tp=2 不可行，回落 tp=1。"""
        cards = [self._card(80.0, 80.0), self._card(24.0, 24.0)]
        assert feasible_tp_for_node(cards, required_vram_gb=33.0) == 1

    def test_empty_or_unknown_requirement(self):
        assert feasible_tp_for_node([], required_vram_gb=10.0) is None
        assert feasible_tp_for_node([self._card(24.0, 24.0)], required_vram_gb=None) is None

    def test_occupied_card_not_recommended(self):
        """一张卡只推荐一个部署：可用量装不下整卡预留的卡不参与推荐。

        两张 80GB 里有一张被占了 10GB（可用 70 < 预留 72）→ 只剩一张可用，推 tp=1。
        旧口径按 ``可用 × 0.9``（70×0.9=63 ≥ 60/2）会把这张卡算进来、推出 tp=2，而调度
        按默认口径的整卡预留必拦下。
        """
        cards = [self._card(80.0, 80.0), self._card(80.0, 70.0)]
        assert feasible_tp_for_node(cards, required_vram_gb=60.0) == 1

    def test_all_cards_too_occupied_returns_none(self):
        """每张卡的可用量都不到自己要预留的量 → 该节点跑不了（不是「tp=1 可行」）。

        默认口径下就是「每张卡都被占用超过 10%」——真实集群里极常见，早先会在这里越界崩溃。
        """
        cards = [self._card(80.0, 25.0)]  # 预留 72 > 可用 25
        assert feasible_tp_for_node(cards, required_vram_gb=20.0) is None


class TestPickCluster:
    def _gpu(self, uuid, node, vram=24.0, avail=None, arch="nvidia"):
        return GpuPoolEntry(
            uuid=uuid,
            node_id=node,
            arch=arch,
            total_vram_gb=vram,
            available_vram_gb=avail if avail is not None else vram,
        )

    def test_single_node_feasible(self):
        """一个集群有节点满足 tp 张卡 → cross_node=False。"""
        pools = {
            1: [self._gpu("g0", "n1"), self._gpu("g1", "n1")],
        }
        pick = pick_cluster(pools, vram_per_gpu=10.0, tp=2)
        assert pick is not None
        assert pick.cluster_id == 1
        assert pick.cross_node is False

    def test_cross_node_feasible(self):
        """集群无单节点够 tp，但跨机够 → cross_node=True。"""
        pools = {
            1: [self._gpu("g0", "n1"), self._gpu("g1", "n2")],
        }
        pick = pick_cluster(pools, vram_per_gpu=10.0, tp=2)
        assert pick is not None
        assert pick.cluster_id == 1
        assert pick.cross_node is True

    def test_single_node_preferred_over_cross_node(self):
        """集群 A 单节点可行，集群 B 跨机可行 → 选 A。"""
        pools = {
            1: [self._gpu("a0", "n1"), self._gpu("a1", "n1")],  # 单节点可行
            2: [self._gpu("b0", "n2"), self._gpu("b1", "n3")],  # 仅跨机
        }
        pick = pick_cluster(pools, vram_per_gpu=10.0, tp=2)
        assert pick is not None
        assert pick.cluster_id == 1
        assert pick.cross_node is False

    def test_no_feasible_returns_none(self):
        """所有集群都不够 → None。"""
        pools = {
            1: [self._gpu("g0", "n1")],  # 只有 1 张卡，tp=2 不够
        }
        pick = pick_cluster(pools, vram_per_gpu=10.0, tp=2)
        assert pick is None

    def test_shape_aware_picks_cluster_that_only_multi_card_fits(self):
        """单卡装不下、同节点两张卡合起来装得下的集群，旧口径整片漏掉。

        旧口径拿 tp=1 的整卡需求（= 模型总量）过滤每张卡，两张 24GB 卡谁都过不了；
        新口径问「这个节点用某个合法 tp 装得下吗」→ tp=2 可行。
        """
        pools = {1: [self._gpu("g0", "n1"), self._gpu("g1", "n1")]}
        shape = ParallelShape(num_attention_heads=32, num_kv_heads=8)
        assert pick_cluster(pools, vram_per_gpu=33.0, tp=1) is None  # 旧口径
        pick = pick_cluster(pools, vram_per_gpu=33.0, tp=1, shape=shape)
        assert pick is not None and pick.cluster_id == 1 and pick.cross_node is False

    def test_shape_aware_still_none_when_truly_too_big(self):
        pools = {1: [self._gpu("g0", "n1"), self._gpu("g1", "n1")]}
        shape = ParallelShape(num_attention_heads=32, num_kv_heads=8)
        pick = pick_cluster(pools, vram_per_gpu=200.0, tp=1, shape=shape)
        assert pick is None

    def test_pinned_tp_ignores_shape_aware_path(self):
        """用户已固定 tp=2 → 仍走旧口径（尊重用户形状选择），不按推导出的 tp 重判。"""
        pools = {1: [self._gpu("g0", "n1"), self._gpu("g1", "n2")]}
        shape = ParallelShape(num_attention_heads=32, num_kv_heads=8)
        pick = pick_cluster(pools, vram_per_gpu=10.0, tp=2, shape=shape)
        assert pick is not None and pick.cross_node is True

    def test_vram_filter(self):
        """容量不合格的卡被过滤：判据同调度（整卡预留 0.9×total 装得下每卡需求、且实时可用
        罩得住预留）——可用 20G 虽 > 每卡需求 5G，但罩不住预留 21.6G 一样不参与。"""
        pools = {
            1: [
                self._gpu("g0", "n1", vram=24.0, avail=5.0),  # 可用 5G → 过滤
                self._gpu("g1", "n1", vram=24.0, avail=20.0),  # 可用 20G < 预留 21.6G → 过滤
            ],
        }
        pick = pick_cluster(pools, vram_per_gpu=10.0, tp=2)
        assert pick is None  # 无合格卡

    def test_explicit_tp_splits_per_card_need(self):
        """显式 tp=2 时每卡门槛 = 整模型需求 ÷ tp，不拿整模型需求当单卡门槛。

        30GB 模型 + 24GB 卡：旧口径拿 30 过滤每张卡 → 集群被判不可行（预览整片空白）；
        按 tp=2 每卡 15GB、整卡预留 21.6 ≥ 15、可用 24 ≥ 21.6 → 单节点可行。
        """
        pools = {1: [self._gpu("g0", "n1"), self._gpu("g1", "n1")]}
        pick = pick_cluster(pools, vram_per_gpu=30.0, tp=2)
        assert pick is not None and pick.cluster_id == 1 and pick.cross_node is False

    def test_occupied_card_not_qualified(self):
        """被占超过一成的卡不参与集群判定（推荐按实时可用判，不按总量乐观）。"""
        pools = {1: [self._gpu("g0", "n1", avail=12.0)]}  # 可用 12 < 预留 21.6
        assert pick_cluster(pools, vram_per_gpu=10.0, tp=1) is None

    def test_arch_filter(self):
        """arch 不匹配的卡被过滤。"""
        pools = {
            1: [
                self._gpu("g0", "n1", arch="cann"),
                self._gpu("g1", "n1", arch="cann"),
            ],
        }
        pick = pick_cluster(pools, vram_per_gpu=10.0, tp=2, arch="nvidia")
        assert pick is None  # 无 nvidia 卡

    def test_empty_pools(self):
        pick = pick_cluster({}, vram_per_gpu=10.0, tp=1)
        assert pick is None
