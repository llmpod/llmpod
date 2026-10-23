"""Topology units×copies 归一化 + 手选多副本分组 + inspect ReplicaPlan 组装的集成测试。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.schemas import Topology, set_topology_copies
from llmpod.apps.manager.deployment.scheduler.gpu import pair_manual_replicas
from llmpod.apps.manager.deployment.scheduler.topology import validate_topology
from llmpod.core.exceptions import LLMPodValidationError


def _gpu(uuid: str, node: str, arch: str = "nvidia", vram: float = 24.0):
    from llmpod.apps.webserver.schemas.deployment_inspect import ClusterGpuInfo
    from llmpod.config.constants import GpuStatus

    return ClusterGpuInfo(
        uuid=uuid,
        node_id=node,
        index=0,
        arch=arch,
        total_vram_gb=vram,
        available_vram_gb=vram,
        status=GpuStatus.ONLINE,
    )


class TestTopologyUnitsCopies:
    """两种写法归一 / 冲突拒 / v1 闸 / dump 双写。"""

    def test_old_shape_normalizes(self):
        t = Topology(tp=2, pp=1, replicas=3)
        assert t.copies == 3
        assert len(t.units) == 1
        assert t.units[0].tp == 2
        assert t.gpus_per_engine() == 2
        assert t.total_gpus() == 6

    def test_new_shape_explicit_fields(self):
        t = Topology(units=[{"role": "full", "tp": 2, "pp": 1, "ep": 1, "nnodes": 1}], copies=3)
        assert t.tp == 2 and t.replicas == 3
        assert "units" in (t.explicit_fields or set())

    def test_dump_double_writes(self):
        d = Topology(tp=2, replicas=3).model_dump()
        assert d["units"][0]["tp"] == 2 and d["copies"] == 3
        assert d["tp"] == 2 and d["replicas"] == 3

    def test_dump_roundtrip(self):
        t = Topology(tp=2, replicas=3)
        t2 = Topology(**t.model_dump())
        assert (t2.tp, t2.replicas, t2.units, t2.copies) == (t.tp, t.replicas, t.units, t.copies)

    def test_conflict_rejected(self):
        with pytest.raises(Exception, match="冲突"):
            Topology(units=[{"tp": 2}], copies=2, tp=4)

    def test_multi_unit_gate(self):
        with pytest.raises(Exception, match="暂未开放"):
            Topology(units=[{"tp": 2}, {"tp": 2}], copies=1)

    def test_pd_role_gate(self):
        with pytest.raises(Exception, match="暂未开放"):
            Topology(units=[{"role": "prefill", "tp": 2}], copies=1)

    def test_units_without_copies_rejected(self):
        with pytest.raises(Exception, match="同时"):
            Topology(units=[{"tp": 2}])

    def test_copies_list_explicit_null_backfilled(self):
        """异构写法 + 显式 null 的旧键（JS 可空键序列化常见）→ 按折算值回填，不穿透。

        回归：旧实现用 setdefault 回填，null 穿透归一化随 spec 落库，下游
        DeploymentSummary(replicas: int) 读到 null 抛 ValidationError → 该集群下
        所有部署的 topology 接口 500。
        """
        t = Topology(copies=[{"tp": 1}, {"tp": 2}], replicas=None, tp=None)
        assert t.replicas == 2 and t.tp == 1
        assert isinstance(t.copies, list) and [u.tp for u in t.copies] == [1, 2]

    def test_legacy_four_field_explicit_null_backfilled(self):
        """旧四数写法 + 显式 null（nnodes=None）同样回填——归一化分支一个口径。

        回归：旧四数分支漏改（仍 setdefault），null 穿透后 DeploymentSummary(nnodes: int)
        读到 None 抛 ValidationError → topology 接口 500。
        """
        t = Topology(tp=1, replicas=None, nnodes=None)
        assert t.replicas == 1
        assert t.nnodes == 1  # 折算回填，不穿透 null
        assert t.tp == 1 and t.pp == 1 and t.ep == 1

    def test_zero_values_pass_through_to_struct_validation(self):
        """nnodes=0 不被归一兜成 1——validate_topology 按结构错误拒。"""
        with pytest.raises(LLMPodValidationError, match="nnodes"):
            validate_topology(Topology(tp=4, nnodes=0))

    def test_default_explicit_fields_empty(self):
        assert Topology(max_model_len=8192).explicit_fields == set()


class TestPairManualReplicas:
    """手选多副本分组：贪心配份 / 余卡拒 / 混 arch 跨份。"""

    def test_pairs_by_largest_bucket(self):
        pool = [_gpu("g1", "n1"), _gpu("g2", "n1"), _gpu("g3", "n1"), _gpu("g4", "n1"), _gpu("g5", "n2")]
        with pytest.raises(LLMPodValidationError, match="余"):
            pair_manual_replicas(pool, ["g1", "g2", "g3", "g4", "g5"], per_copy=2)
        pairs = pair_manual_replicas(pool, ["g1", "g2", "g3", "g4"], per_copy=2)
        assert len(pairs) == 2
        assert all(arch == "nvidia" for arch, _ in pairs)
        flat = sorted(u for _, us in pairs for u in us)
        assert flat == ["g1", "g2", "g3", "g4"]

    def test_leftover_rejected_with_reason(self):
        pool = [_gpu("g1", "n1"), _gpu("g2", "n1"), _gpu("g3", "n2")]
        with pytest.raises(LLMPodValidationError, match="余"):
            pair_manual_replicas(pool, ["g1", "g2", "g3"], per_copy=2)

    def test_mixed_arch_pairs_across_replicas(self):
        """异构份：nvidia 份与 cann 份各自成对（桶按 (node, arch) 分，互不混）。"""
        pool = [
            _gpu("n1-g1", "n1"),
            _gpu("n1-g2", "n1"),
            _gpu("n2-c1", "n2", arch="cann"),
            _gpu("n2-c2", "n2", arch="cann"),
        ]
        pairs = pair_manual_replicas(pool, ["n1-g1", "n1-g2"], per_copy=2)
        assert len(pairs) == 1
        # 不同 (node, arch) 桶互不混 → 跨桶选集凑不出份
        with pytest.raises(LLMPodValidationError):
            pair_manual_replicas(pool, ["n1-g1", "n2-c1"], per_copy=2)
        cann = pair_manual_replicas(pool, ["n2-c1", "n2-c2"], per_copy=2)
        assert cann == [("cann", ["n2-c1", "n2-c2"])]


class TestSetTopologyCopies:
    """spec dict 的份数回写（scale 落库用）：模板化形态两键同步，旧四数形态不造非法混合。"""

    def test_template_form_writes_both_keys(self):
        topo = Topology(tp=1, replicas=2).model_dump()
        merged = set_topology_copies(topo, 3)
        assert merged["replicas"] == 3 and merged["copies"] == 3
        assert Topology(**merged).replicas == 3  # 读回不冲突

    def test_legacy_form_keeps_single_key(self):
        """旧四数形态（无 units/copies）只写 replicas——补 copies 会造出读不回的混合形态。"""
        legacy = {"tp": 2, "pp": 1, "ep": 1, "replicas": 1, "nnodes": 1}
        merged = set_topology_copies(legacy, 3)
        assert merged["replicas"] == 3
        assert "copies" not in merged
        assert Topology(**merged).replicas == 3
