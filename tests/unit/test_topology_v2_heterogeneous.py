"""拓扑形状 v2：异构副本（copies 列表）+ pp 跨机整除钩子 + 逐份显存口径。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.engine.sglang import SGLangAdapter
from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter
from llmpod.apps.manager.deployment.schemas import Topology
from llmpod.apps.manager.deployment.scheduler.gpu import (
    _validate_manual_selection_shapes,
    pair_manual_replicas,
)
from llmpod.apps.manager.deployment.scheduler.topology import expand_standard, validate_topology
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


class TestHeterogeneousNormalization:
    """copies 列表写法：归一 / 同形折叠 / 冲突矩阵 / dump 回环。"""

    def test_hetero_normalizes(self):
        t = Topology(copies=[{"tp": 1}, {"tp": 2, "nnodes": 2}])
        assert not t.is_homogeneous
        assert t.replicas == 2
        assert [u.tp for u in t.per_copy_units()] == [1, 2]
        assert [u.nnodes for u in t.per_copy_units()] == [1, 2]
        # units/旧四数 = 首份折算（兼容读点）
        assert t.units[0].tp == 1 and t.tp == 1 and t.nnodes == 1
        assert t.total_gpus() == 3

    def test_homo_list_collapses_to_int(self):
        """全同模板的列表归一为 int 形态（dump 与同形写法一致，前端读点无感）。"""
        t = Topology(copies=[{"tp": 2}, {"tp": 2}])
        assert t.is_homogeneous
        assert t.copies == 2 and t.replicas == 2 and t.tp == 2
        assert Topology(**t.model_dump()).copies == 2

    def test_hetero_dump_roundtrip(self):
        t = Topology(copies=[{"tp": 1}, {"tp": 3}])
        t2 = Topology.model_validate(t.model_dump())
        assert not t2.is_homogeneous
        assert [u.tp for u in t2.per_copy_units()] == [1, 3]

    def test_units_echo_first_copy_allowed(self):
        """units = 首份模板（本类 dump 的回环形态）放行；其它值 = 冲突拒。"""
        Topology(copies=[{"tp": 1}, {"tp": 2}], units=[{"tp": 1}])
        with pytest.raises(Exception, match="冲突"):
            Topology(copies=[{"tp": 1}, {"tp": 2}], units=[{"tp": 3}])

    def test_old_fields_conflict_with_list(self):
        with pytest.raises(Exception, match="冲突"):
            Topology(copies=[{"tp": 1}, {"tp": 2}], tp=5)
        # 一致的旧字段放行（折算对得上）
        Topology(copies=[{"tp": 1}, {"tp": 2}], tp=1, replicas=2)

    def test_empty_list_and_pd_role_rejected(self):
        with pytest.raises(Exception, match="不能为空"):
            Topology(copies=[])
        with pytest.raises(Exception, match="暂未开放"):
            Topology(copies=[{"role": "prefill", "tp": 1}])

    def test_with_updates_rejects_heterogeneous(self):
        t = Topology(copies=[{"tp": 1}, {"tp": 2}])
        with pytest.raises(ValueError, match="异构"):
            t.with_updates(replicas=3)


class TestPerCopyValidationAndExpand:
    """逐份校验 / 展开 / 跨机整除引擎分叉。"""

    def test_vllm_pp_participates_cross_node(self):
        """vLLM torch-elastic：tp×pp 整除 nnodes 即可（tp=1/pp=2/nnodes=2 合法）。"""
        insts = VLLMAdapter.expand_topology(Topology(tp=1, pp=2, nnodes=2))
        assert [(i.role, i.node_rank, i.tp, i.pp) for i in insts] == [("head", 0, 1, 2), ("worker", 1, 1, 2)]

    def test_sglang_pp_participates_cross_node(self):
        """SGLang 钩子按引擎分区规则：主推形态（pp=nnodes）放行；总卡数整除但分不开的
        形状（tp=2/pp=3/nnodes=2）拒——引擎会静默按另一个每节点卡数拉起。"""
        SGLangAdapter.expand_topology(Topology(tp=1, pp=2, nnodes=2))  # 纯 PP 跨机：过
        SGLangAdapter.expand_topology(Topology(tp=2, pp=2, nnodes=2))  # 混合：过
        with pytest.raises(LLMPodValidationError, match="均分"):
            SGLangAdapter.expand_topology(Topology(tp=1, pp=2, nnodes=3))
        with pytest.raises(LLMPodValidationError, match="均分"):
            SGLangAdapter.expand_topology(Topology(tp=2, pp=3, nnodes=2))

    def test_vllm_bad_divisibility_rejected(self):
        with pytest.raises(LLMPodValidationError, match="tp×pp"):
            VLLMAdapter.expand_topology(Topology(tp=1, pp=1, nnodes=2))

    def test_no_adapter_keeps_default_rule(self):
        validate_topology(Topology(tp=4, nnodes=2))  # tp 整除 → 过
        with pytest.raises(LLMPodValidationError, match="必须能被"):
            validate_topology(Topology(tp=1, pp=2, nnodes=2))

    def test_hetero_expand_per_copy(self):
        """38GB demo 形状：份1 单机 tp1 + 份2 跨机 tp2 → 3 实例（full + head + worker）。"""
        t = Topology(copies=[{"tp": 1}, {"tp": 2, "nnodes": 2}])
        insts = expand_standard(t, adapter=VLLMAdapter)
        assert [(i.replica_idx, i.role, i.tp, i.nnodes) for i in insts] == [
            (0, "full", 1, 1),
            (1, "head", 2, 2),
            (1, "worker", 2, 2),
        ]

    def test_hetero_error_carries_copy_index(self):
        t = Topology(copies=[{"tp": 1}, {"tp": 1, "nnodes": 0}])
        with pytest.raises(LLMPodValidationError, match="第 2 份"):
            validate_topology(t)

    def test_hetero_node_ids_rejected(self):
        t = Topology(copies=[{"tp": 1}, {"tp": 2}])
        with pytest.raises(LLMPodValidationError, match="异构"):
            validate_topology(t, type("Sel", (), {"node_id": None, "gpu_uuids": None, "node_ids": ["n1"]})())

    def test_homo_messages_unchanged(self):
        """同形场景错误消息不带份序号（历史消息逐字节不变）。"""
        with pytest.raises(LLMPodValidationError, match=r"^topology\.pp 必须"):
            validate_topology(Topology(tp=1, pp=0))


class TestHeterogeneousPairing:
    """异构手选：逐份卡数配对 / 余卡拒 / 总数错拒。"""

    def test_mixed_pair_demo_shape(self):
        """demo：n1 两张（tp2 份）+ n2 一张 cann（tp1 份）→ 各归各份。"""
        pool = [_gpu("a", "n1"), _gpu("b", "n1"), _gpu("c", "n2", arch="cann")]
        pairs = pair_manual_replicas(pool, ["a", "b", "c"], per_copy=[2, 1])
        assert pairs == [("nvidia", ["a", "b"]), ("cann", ["c"])]

    def test_mixed_pair_order_follows_copy_list(self):
        """返回序与 per_copy 列表对齐（配对从最大份取起，但输出按份序）。"""
        pool = [_gpu("a", "n1"), _gpu("b", "n2"), _gpu("c", "n2")]
        pairs = pair_manual_replicas(pool, ["a", "b", "c"], per_copy=[1, 2])
        assert pairs[0] == ("nvidia", ["a"])
        assert pairs[1] == ("nvidia", ["b", "c"])

    def test_mixed_pair_insufficient_bucket_rejected(self):
        pool = [_gpu("a", "n1"), _gpu("b", "n2")]
        with pytest.raises(LLMPodValidationError, match="第 1 份"):
            pair_manual_replicas(pool, ["a", "b"], per_copy=[2, 1])

    def test_shapes_validation_count_mismatch(self):
        pool = [_gpu("a", "n1"), _gpu("b", "n1")]
        with pytest.raises(LLMPodValidationError, match="逐份"):
            _validate_manual_selection_shapes(pool, ["a"], shapes=[(1, 1, 1), (2, 1, 1)])

    def test_shapes_validation_multi_copy_cross_node_rejected(self):
        """多份且任一份跨机 → 手选拒（总数先对齐再触发跨机拒绝）。"""
        pool = [
            _gpu("a", "n1"),
            _gpu("b", "n1"),
            _gpu("c", "n2"),
            _gpu("d", "n3"),
        ]
        # 份1 tp1 + 份2 tp2/nnodes2 = 3 张；多给 1 张会先撞总数，故精确给 3 张
        pool3 = pool[:3]
        with pytest.raises(LLMPodValidationError, match="多副本×跨机"):
            _validate_manual_selection_shapes(pool3, ["a", "b", "c"], shapes=[(1, 1, 1), (2, 1, 2)])


class TestPerCopyVram:
    """逐份每卡显存：异构各份各的口径；同形 = estimate_vram_gb 单值。"""

    def test_per_unit_divides_by_own_parallel(self):
        """无 max_model_len → KV=0（乐观裸权重口径），每卡 = 权重 ÷ 该份 tp×pp。"""
        t = Topology(copies=[{"tp": 1}, {"tp": 2}])
        u1, u2 = t.per_copy_units()
        assert VLLMAdapter.estimate_vram_gb_for_unit(38.0, u1, None, "llm") == 38.0
        assert VLLMAdapter.estimate_vram_gb_for_unit(38.0, u2, None, "llm") == 19.0
        # 带 ctx：38 + KV(38×0.1) = 41.8，再按各份并行度摊
        assert VLLMAdapter.estimate_vram_gb_for_unit(38.0, u1, 8192, "llm") == pytest.approx(41.8, abs=0.01)
        assert VLLMAdapter.estimate_vram_gb_for_unit(38.0, u2, 8192, "llm") == pytest.approx(20.9, abs=0.01)

    def test_estimate_vram_gb_matches_first_copy(self):
        """单值口径 = 首份（历史兼容）。"""
        t = Topology(copies=[{"tp": 1}, {"tp": 2}])
        assert VLLMAdapter.estimate_vram_gb(38.0, t, model_type="llm") == VLLMAdapter.estimate_vram_gb_for_unit(
            38.0, t.per_copy_units()[0], t.max_model_len, "llm"
        )

    def test_pp_divides_vram(self):
        assert VLLMAdapter.estimate_vram_gb(38.0, Topology(tp=1, pp=2), model_type="llm") == 19.0
        assert VLLMAdapter.estimate_vram_gb(38.0, Topology(tp=1, pp=2, max_model_len=8192), model_type="llm") == (
            pytest.approx(20.9, abs=0.01)
        )


class TestDerivationDomainFrozen:
    """推导域冻结不变量：自动推导永不产生 pp>1 / 异构副本 / 跨机 / 多副本。"""

    def test_heterogeneous_never_derived(self):
        from llmpod.apps.manager.deployment.recommend import should_derive_tp

        t = Topology(copies=[{"tp": 1}, {"tp": 2}])
        assert should_derive_tp(None, t, tp_explicit=False, has_pool=True) is False

    def test_pp_multi_node_multi_replica_not_derived(self):
        from llmpod.apps.manager.deployment.recommend import should_derive_tp

        assert should_derive_tp(None, Topology(pp=2), tp_explicit=False, has_pool=True) is False
        assert should_derive_tp(None, Topology(nnodes=2, tp=2), tp_explicit=False, has_pool=True) is False
        assert should_derive_tp(None, Topology(replicas=2), tp_explicit=False, has_pool=True) is False
        # 唯一推导域：单机单副本 pp=1，未显式 tp，有卡池
        assert should_derive_tp(None, Topology(), tp_explicit=False, has_pool=True) is True

    def test_copies_list_counts_as_explicit_tp(self):
        """copies 键出现即视为显式形状（路由 tp_explicit 判定含 copies）。"""
        t = Topology(copies=[{"tp": 1}])
        assert t.explicit_fields == {"copies"}
        t2 = Topology(units=[{"tp": 2}], copies=2)
        assert "units" in t2.explicit_fields and "copies" in t2.explicit_fields


class TestScaleRejectsHeterogeneous:
    """异构部署拒绝伸缩——守卫层单测（DB 流程走 integration）。"""

    def test_guard_message(self):
        from llmpod.apps.manager.deployment.scale import request_scale

        # 直接验证守卫分支：load_scale_context 后 is_homogeneous=False → Conflict
        # （此处不建 DB，静态断言守卫存在与语义由 integration 覆盖；单测锁消息文案）
        import inspect

        src = inspect.getsource(request_scale)
        assert "is_homogeneous" in src and "异构副本部署暂不支持在线伸缩" in src
