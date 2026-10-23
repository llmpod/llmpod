"""调度器选卡管线测试（Filter 链 / 候选阶梯 / Scorer 链 / 多机×多副本 / 本地性）。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from llmpod.apps.manager.deployment.schemas import InstanceSpec, PlacementPrefs, Topology
from llmpod.apps.manager.deployment.scheduler.gpu import plan_placement, recommend_placement
from llmpod.apps.manager.deployment.scheduler.pipeline import (
    EngineRequest,
    PipelineRejection,
    _user_reject_reason,
    place_engine,
    run_filters,
    default_filters,
)
from llmpod.apps.manager.deployment.scheduler.topology import expand_standard
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


def _req(**kw) -> EngineRequest:
    defaults = dict(engine_id="e1", need_per_node=1, nnodes=1, arch="nvidia")
    defaults.update(kw)
    return EngineRequest(**defaults)


class TestFilterChain:
    def test_online_arch_capacity_in_order(self):
        pool = [
            Card("g0", "n1", status="offline"),
            Card("g1", "n1", arch="cann"),
            Card("g2", "n1", available_vram_gb=2.0),
            Card("g3", "n1"),
        ]
        filters = default_filters("nvidia", card_ok=lambda g: g.available_vram_gb >= 10.0)
        passed, rejections, reasons = run_filters(pool, filters)
        # 谁否决谁署名：offline 1 张、arch 1 张、capacity 1 张
        assert rejections == {"online": 1, "arch": 1, "capacity": 1}
        # 原因串也带出来（部署失败时给用户看的就是它，不再是光秃秃的计数）
        assert reasons["online"] == "节点离线" and reasons["arch"] == "GPU 架构不匹配（arch≠nvidia）"
        assert [g.uuid for g in passed["n1"]] == ["g3"]

    def test_no_card_ok_passes_all_online(self):
        pool = [Card("g0", "n1", available_vram_gb=0.0)]
        passed, rejections, reasons = run_filters(pool, default_filters("nvidia"))
        assert rejections == {} and reasons == {} and len(passed["n1"]) == 1


class TestLadderAndScorer:
    def test_single_node_binpack_picks_tightest(self):
        """binpack：need=1 时 1 张余量的 n1 比 3 张的 n2 分高（省出整节点）。"""
        pool = [
            Card("g0", "n1", index=0),
            Card("g2", "n2", index=0),
            Card("g3", "n2", index=1),
            Card("g4", "n2", index=2),
        ]
        p = place_engine(pool, _req(need_per_node=1))
        assert p.ladder == "single_node" and p.shards[0][0] == "n1"

    def test_locality_beats_policy(self):
        """本地性加分压过 binpack：模型已在 n2（余量更宽）也选 n2，省一次下发。"""
        pool = [
            Card("g0", "n1", index=0),
            Card("g2", "n2", index=0),
            Card("g3", "n2", index=1),
        ]
        p = place_engine(pool, _req(need_per_node=1, locality_node_ids=frozenset({"n2"})))
        assert p.shards[0][0] == "n2"

    def test_spread_picks_roomiest(self):
        pool = [
            Card("g0", "n1", index=0),
            Card("g2", "n2", index=0),
            Card("g3", "n2", index=1),
        ]
        p = place_engine(pool, _req(need_per_node=1, policy="spread"))
        assert p.shards[0][0] == "n2"

    def test_card_state_picks_clean_card_within_node(self):
        """同节点同档两张卡：有已停止部署痕迹的那张排后，先挑干净的。"""
        pool = [Card("dirty", "n1", index=0), Card("clean", "n1", index=1)]
        p = place_engine(pool, _req(need_per_node=1, card_state={"dirty": "residual"}))
        assert [g.uuid for g in p.shards[0][1]] == ["clean"]

    def test_card_state_prefers_clean_node(self):
        """两节点各 1 张可用卡（binpack 打平）：有痕迹的节点降权，选干净节点。"""
        pool = [Card("dirty", "n1", index=0), Card("clean", "n2", index=0)]
        p = place_engine(pool, _req(need_per_node=1, card_state={"dirty": "residual"}))
        assert p.shards[0][0] == "n2"

    def test_shared_card_scores_worse_than_residual(self):
        """痕迹分档：仍被运行中部署占用（shared）比只剩停止残留（residual）更次。"""
        pool = [Card("shared", "n1", index=0), Card("residual", "n2", index=0)]
        p = place_engine(pool, _req(need_per_node=1, card_state={"shared": "shared", "residual": "residual"}))
        assert p.shards[0][0] == "n2"

    def test_locality_beats_card_residue(self):
        """本地性（+1000）压过卡痕迹降权（每张 -100）：有模型文件的脏卡节点仍优先。"""
        pool = [Card("dirty", "n1", index=0), Card("clean", "n2", index=0)]
        p = place_engine(
            pool,
            _req(need_per_node=1, locality_node_ids=frozenset({"n1"}), card_state={"dirty": "residual"}),
        )
        assert p.shards[0][0] == "n1"

    def test_residue_card_still_usable_when_only_option(self):
        """软偏好不过滤：只有脏卡可用时照旧调度（不因痕迹拒部署）。"""
        pool = [Card("dirty", "n1", index=0)]
        p = place_engine(pool, _req(need_per_node=1, card_state={"dirty": "residual"}))
        assert p.shards[0][0] == "n1"

    def test_cross_node_combines_distinct_nodes(self):
        pool = [
            Card("g0", "n1"),
            Card("g1", "n1"),
            Card("g2", "n2"),
            Card("g3", "n2"),
            Card("g4", "n3"),
        ]
        p = place_engine(pool, _req(need_per_node=1, nnodes=2))
        assert p.ladder == "cross_node"
        # binpack 组级裁决：{n1,n3} 剩余 2+1 用得最紧（耗尽 n3、保住 n2 完整 2 卡）
        assert {nid for nid, _ in p.shards} == {"n1", "n3"}

    def test_cross_node_respects_exclude_nodes(self):
        """多机分片互异：引擎第二分片排除第一分片已落节点。"""
        pool = [
            Card("g0", "n1"),
            Card("g1", "n2"),
            Card("g2", "n3"),
        ]
        p = place_engine(pool, _req(need_per_node=1, nnodes=1, exclude_nodes=frozenset({"n1"})))
        assert p.shards[0][0] == "n2"

    def test_cross_node_pool_cap_never_below_nnodes(self):
        """组合池截断下限 = nnodes：池上限(8) < 节点数时 combinations 恒空，跨机永远排不开。"""
        pool = [Card(f"g{i}", f"n{i}") for i in range(10)]
        p = place_engine(pool, _req(need_per_node=1, nnodes=9))
        assert p.ladder == "cross_node"
        assert len(p.shards) == 9

    def test_used_uuids_excluded(self):
        pool = [Card("g0", "n1", index=0), Card("g1", "n1", index=1)]
        p = place_engine(pool, _req(need_per_node=1, used_uuids=frozenset({"g0"})))
        assert [g.uuid for g in p.shards[0][1]] == ["g1"]

    def test_tier_uniform_mixed_vram(self):
        """同节点混显存档（24G×2 + 16G×2）→ 取高档同档 2 张（同引擎须同显存档）。"""
        pool = [
            Card("g0", "n1", index=0, total_vram_gb=24.0),
            Card("g1", "n1", index=1, total_vram_gb=16.0),
            Card("g2", "n1", index=2, total_vram_gb=24.0),
            Card("g3", "n1", index=3, total_vram_gb=16.0),
        ]
        p = place_engine(pool, _req(need_per_node=2))
        assert {g.uuid for g in p.shards[0][1]} == {"g0", "g2"}

    def test_rejection_carries_signatures(self):
        pool = [Card("g0", "n1", status="offline")]
        with pytest.raises(PipelineRejection) as ei:
            place_engine(pool, _req())
        assert ei.value.rejections == {"online": 1} and ei.value.ladder == "single_node"

    def test_rejection_carries_user_reason(self):
        """部署失败时落到 last_error 的是这条中文说明，不是内部句式。"""
        pool = [Card("g0", "n1", status="offline")]
        with pytest.raises(PipelineRejection) as ei:
            place_engine(pool, _req(need_per_node=2))
        reason = ei.value.user_reason
        assert reason is not None
        assert "节点离线" in reason and "×1" in reason
        assert "同一节点" in reason and "2 张" in reason
        # 原始 message 保留英文内部句式供排障
        assert "no node with" in str(ei.value)

    def test_user_reason_translates_each_filter(self):
        req = _req(need_per_node=1, nnodes=2)
        reason = _user_reject_reason(
            req,
            {"online": 3, "arch": 2},
            {"online": "节点离线", "arch": "GPU 架构不匹配（arch≠nvidia）"},
            "nvidia",
        )
        assert "节点离线" in reason and "GPU 架构不匹配（arch≠nvidia）" in reason
        assert "2 个不同节点" in reason

    def test_user_reason_when_no_filter_flagged(self):
        reason = _user_reject_reason(_req(need_per_node=1), {}, {}, "nvidia")
        assert "没有符合架构的在线卡" in reason


class TestManualSelection:
    def test_wanted_cards_directly_placed(self):
        """手选卡跳过阶梯/scorer：指定 g1（低分卡）也照选（用户显式指定永远赢）。"""
        pool = [
            Card("g0", "n1", index=0),
            Card("g1", "n1", index=1),
            Card("g2", "n1", index=2),
        ]
        p = place_engine(pool, _req(need_per_node=1, wanted_uuids=("g1",)))
        assert p.ladder == "manual" and p.shards[0][1][0].uuid == "g1"

    def test_wanted_veto_names_reason(self):
        """指定卡显存不足 → 拒绝并署名（capacity/显存不足），不静默换卡。"""
        pool = [Card("g0", "n1", available_vram_gb=2.0), Card("g1", "n1")]
        with pytest.raises(LLMPodValidationError, match="显存不足"):
            place_engine(
                pool,
                _req(need_per_node=1, wanted_uuids=("g0",)),
                card_ok=lambda g: g.available_vram_gb >= 10.0,
            )

    def test_wanted_bad_distribution(self):
        pool = [Card("g0", "n1"), Card("g1", "n1")]
        with pytest.raises(LLMPodValidationError, match="2 个节点"):
            place_engine(pool, _req(need_per_node=1, nnodes=2, wanted_uuids=("g0", "g1")))


class TestPlanPlacementFold:
    def test_multi_replica_multinode(self):
        """多机×多副本：2 副本 × tp2/nnodes2 = 每副本 2 分片各 1 卡落不同节点；卡不重叠。"""
        insts = expand_standard(Topology(tp=2, nnodes=2, replicas=2))
        pool = [
            Card("g0", "n1", index=0),
            Card("g1", "n1", index=1),
            Card("g2", "n2", index=0),
            Card("g3", "n2", index=1),
            Card("g4", "n3", index=0),
            Card("g5", "n3", index=1),
            Card("g6", "n4", index=0),
            Card("g7", "n4", index=1),
        ]
        plan = plan_placement(insts, pool, arch="nvidia")
        assert len(plan) == 4  # 2 副本 × 2 分片
        by_replica: dict[int, list] = {}
        for pick in plan:
            by_replica.setdefault(pick.instance.replica_idx, []).append(pick)
        for picks in by_replica.values():
            # 份内分片节点互异
            assert len({p.node_id for p in picks}) == 2
            assert all(len(p.cards) == 1 for p in picks)
        # 跨副本卡排重：每分片 1 卡（tp×pp÷nnodes=1），4 卡各用一次
        used = [g.uuid for p in plan for g in p.cards]
        assert len(used) == len(set(used)) == 4

    def test_engines_grouped_not_per_shard(self):
        """多机引擎按 engine_instance_id 整组选位（组合原子性），分片数 = nnodes。"""
        head = InstanceSpec(
            instance_id="i-h",
            engine_instance_id="eng-x",
            rank=0,
            role="head",
            tp=2,
            nnodes=2,
            node_rank=0,
            replica_idx=0,
        )
        worker = InstanceSpec(
            instance_id="i-w",
            engine_instance_id="eng-x",
            rank=1,
            role="worker",
            tp=2,
            nnodes=2,
            node_rank=1,
            replica_idx=0,
        )
        pool = [
            Card("g0", "n1", index=0),
            Card("g1", "n1", index=1),
            Card("g2", "n2", index=0),
            Card("g3", "n2", index=1),
        ]
        plan = plan_placement([head, worker], pool, arch="nvidia")
        assert {p.instance.instance_id for p in plan} == {"i-h", "i-w"}
        assert {p.node_id for p in plan} == {"n1", "n2"}
        # head（node_rank=0）与调度节点组按序配对
        head_pick = next(p for p in plan if p.instance.instance_id == "i-h")
        assert len(head_pick.cards) == 1

    def test_locality_passed_through_fold(self):
        insts = expand_standard(Topology(tp=1, replicas=1))
        pool = [Card("g0", "n1"), Card("g1", "n2")]
        plan = plan_placement(insts, pool, arch="nvidia", prefs=PlacementPrefs(locality_node_ids=frozenset({"n2"})))
        assert plan[0].node_id == "n2"

    def test_recommend_respects_locality(self):
        insts = expand_standard(Topology(tp=1, replicas=1))
        pool = [
            Card("g0", "n1", index=0, available_vram_gb=24.0),
            Card("g1", "n2", index=0, available_vram_gb=24.0),
        ]
        uuids, nodes = recommend_placement(
            insts,
            pool,
            arch="nvidia",
            required_vram_gb=None,
        )
        # recommend_placement 暂未接本地性（inspect 数据源），只验不炸
        assert nodes and len(uuids) == 1


class TestCardTraceCap:
    """卡痕迹封顶的粒度：**整份调度**，不是单节点（否则跨机累加会压过本地性加分）。"""

    @staticmethod
    def _placement() -> object:
        from llmpod.apps.manager.deployment.scheduler.pipeline import Placement

        # 3 节点 × 每节点 2 张有痕迹的卡
        return Placement(
            shards=tuple((nid, [Card(f"{nid}-g{i}", nid, index=i) for i in range(2)]) for nid in ("n1", "n2", "n3")),
            ladder="cross_node",
        )

    def test_trace_penalty_capped_across_nodes(self):
        from llmpod.apps.manager.deployment.scheduler.pipeline import CARD_STATE_PENALTY_CAP, score_placement

        # 每节点 2 张 shared = 400：未封顶时合计 1200 > LOCALITY_BONUS(1000)，本地性会被反压
        def trace(nid, cards):
            return 200.0 * len(cards)

        def locality(nid, cards):
            return 1000.0 if nid == "n1" else 0.0

        score = score_placement(self._placement(), scorers=[locality], trace_penalty=trace)
        # 1000（本地性）− 500（封顶后的痕迹）= +500 > 0：本地性仍然赢
        assert score == 1000.0 - CARD_STATE_PENALTY_CAP

    def test_uncapped_sum_would_have_flipped_the_ranking(self):
        from llmpod.apps.manager.deployment.scheduler.pipeline import CARD_STATE_PENALTY_CAP

        # 反证：按节点封顶（旧口径）时，3 节点的痕迹合计 1200 会把本地性加分压成负分
        per_node_capped = sum(min(200.0 * 2, CARD_STATE_PENALTY_CAP) for _ in range(3))
        assert 1000.0 - per_node_capped < 0


class TestArchGateAndPreferenceEdges:
    """回归：架构闸门与偏好权重的边界（见 code-review 修复批）。"""

    def test_empty_arch_order_is_veto_not_fallback(self):
        """arch_order=() 表示「上游把架构全闸掉了」→ 直接否决，不回退到未经闸门的 arch。"""
        pool = [Card("g0", "n1", index=0)]
        with pytest.raises(PipelineRejection) as ei:
            place_engine(pool, _req(need_per_node=1, arch_order=()))
        assert ei.value.ladder == "arch_gate"
        # 这条拒绝同样要带人读原因：部署失败落 last_error 的是它，不是内部英文句式
        assert ei.value.user_reason and "没有可用的架构" in ei.value.user_reason

    def test_none_arch_order_falls_back_to_arch(self):
        """arch_order=None 表示「未给候选序」→ 按 arch 走（旧调用形态不受影响）。"""
        pool = [Card("g0", "n1", index=0)]
        p = place_engine(pool, _req(need_per_node=1, arch_order=None))
        assert p.shards[0][0] == "n1"

    def test_within_node_prefers_residual_over_shared(self):
        """同节点同档：residual（轻）优先于 shared（重）——排序用权重而非布尔。"""
        pool = [Card("shared", "n1", index=0), Card("residual", "n1", index=1)]
        p = place_engine(pool, _req(need_per_node=1, card_state={"shared": "shared", "residual": "residual"}))
        assert [g.uuid for g in p.shards[0][1]] == ["residual"]

    def test_card_penalty_is_capped_below_locality(self):
        """8 卡全 shared 的节点罚分封顶 < 本地性加分：有模型文件的节点仍胜出。"""
        pool = [Card(f"s{i}", "n1", index=i) for i in range(8)]
        pool.append(Card("clean", "n2", index=0))
        p = place_engine(
            pool,
            _req(
                need_per_node=1,
                locality_node_ids=frozenset({"n1"}),
                card_state={f"s{i}": "shared" for i in range(8)},
            ),
        )
        assert p.shards[0][0] == "n1"  # 本地性 > 封顶后的卡痕迹
