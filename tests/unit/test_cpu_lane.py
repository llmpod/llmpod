"""CPU / 混合 offload 部署单测：调度（RAM binpack）、容量判定、拓扑约束。

纯函数无 DB；编排器分支的端到端行为见 tests/integration/test_deployment_flow.py。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmpod.apps.manager.deployment.scheduler.gpu import assess_cpu_capacity, plan_cpu_placement
from llmpod.apps.manager.deployment.scheduler.topology import validate_cpu_lane
from llmpod.core.exceptions import LLMPodValidationError


def _node(node_id: str, memory_gb: float):
    return SimpleNamespace(node_id=node_id, memory_gb=memory_gb, hostname=node_id, ip="127.0.0.1")


def _inst(instance_id: str, rank: int = 0):
    return SimpleNamespace(instance_id=instance_id, rank=rank)


class TestPlanCpuPlacement:
    def test_best_fit_prefers_tightest_node(self):
        """binpack：能放下且余量最小的节点优先（小节点先填满）。"""
        picks = plan_cpu_placement([_inst("i1")], [_node("big", 256.0), _node("tight", 40.0)], ram_per_instance_gb=30.0)
        assert picks == {"i1": "tight"}

    def test_spreads_when_single_node_insufficient(self):
        """单节点放不下两份 → 第二份落到另一个节点。"""
        picks = plan_cpu_placement(
            [_inst("i1"), _inst("i2")],
            [_node("n1", 40.0), _node("n2", 40.0)],
            ram_per_instance_gb=30.0,
        )
        assert set(picks.values()) == {"n1", "n2"}

    def test_raises_when_no_node_fits(self):
        with pytest.raises(LLMPodValidationError):
            plan_cpu_placement([_inst("i1")], [_node("n1", 16.0)], ram_per_instance_gb=30.0)

    def test_ledger_claims_reduce_capacity(self):
        class _Ledger:
            def get_available_ram(self, node_id, total_gb):
                return total_gb * 0.9 - (35.0 if node_id == "n1" else 0.0)

        # n1 被占了 35GB → 只剩 1GB，放不下 30GB；n2 空闲 → 落 n2
        picks = plan_cpu_placement(
            [_inst("i1")], [_node("n1", 40.0), _node("n2", 40.0)], ram_per_instance_gb=30.0, ledger=_Ledger()
        )
        assert picks == {"i1": "n2"}

    def test_force_places_on_roomiest_when_none_fits(self):
        """强制部署：无处可放不拒，退化为「可用内存最大节点」超额调度。"""
        picks = plan_cpu_placement(
            [_inst("i1")], [_node("small", 16.0), _node("big", 40.0)], ram_per_instance_gb=100.0, force=True
        )
        assert picks == {"i1": "big"}

    def test_force_still_raises_without_nodes(self):
        with pytest.raises(LLMPodValidationError):
            plan_cpu_placement([_inst("i1")], [], ram_per_instance_gb=1.0, force=True)


class TestAssessCpuCapacity:
    def test_ok_when_enough_nodes(self):
        ok, reason = assess_cpu_capacity([_node("n1", 64.0), _node("n2", 64.0)], replicas=2, ram_per_replica_gb=30.0)
        assert ok is True and reason is None

    def test_rejects_when_nodes_too_small(self):
        ok, reason = assess_cpu_capacity([_node("n1", 16.0)], replicas=1, ram_per_replica_gb=30.0)
        assert ok is False and "内存" in reason

    def test_rejects_when_no_nodes(self):
        ok, reason = assess_cpu_capacity([], replicas=1, ram_per_replica_gb=1.0)
        assert ok is False and "无在线节点" in reason


class TestValidateCpuLane:
    def test_single_full_unit_ok(self):
        from llmpod.apps.manager.deployment.schemas import Topology

        validate_cpu_lane(Topology(tp=1, pp=1, nnodes=1, replicas=2))

    def test_tp_pp_nnodes_must_be_one(self):
        from llmpod.apps.manager.deployment.schemas import Topology

        for kw in ({"tp": 2}, {"pp": 2}, {"nnodes": 2}):
            with pytest.raises(LLMPodValidationError):
                validate_cpu_lane(Topology(**kw))

    def test_heterogeneous_rejected(self):
        from llmpod.apps.manager.deployment.schemas import Topology
        from llmpod.apps.manager.deployment.schemas.config import UnitTemplate

        topo = Topology(copies=[UnitTemplate(role="full", tp=1), UnitTemplate(role="full", tp=2)])
        with pytest.raises(LLMPodValidationError):
            validate_cpu_lane(topo)
