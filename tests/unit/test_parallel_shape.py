"""并行度形状校验：tp 切开模型的判定与消息（vLLM 语义口径）。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment import parallel
from llmpod.apps.manager.deployment.schemas import Topology
from llmpod.core.exceptions import LLMPodValidationError

from llmpod.apps.manager.deployment.parallel import ParallelShape


class TestTpOk:
    def test_tp_one_always_ok(self):
        assert parallel.tp_ok(ParallelShape(num_attention_heads=32, num_kv_heads=8), 1) is True

    def test_unknown_shape_passes_anything(self):
        # 未探测过的老模型行：宁可漏报不误拒
        assert parallel.tp_ok(ParallelShape(), 3) is True
        assert parallel.tp_ok(ParallelShape(num_attention_heads=None, num_kv_heads=None), 7) is True

    def test_attention_heads_divides(self):
        shape = ParallelShape(num_attention_heads=32)
        assert parallel.tp_ok(shape, 4) is True
        assert parallel.tp_ok(shape, 3) is False

    def test_kv_equal_to_tp(self):
        assert parallel.tp_ok(ParallelShape(num_attention_heads=32, num_kv_heads=8), 8) is True

    def test_kv_not_dividing_tp_is_rejected(self):
        # heads 过得去、KV 过不去 —— vLLM 自己不在 config 期查这条，会在建层期裸 AssertionError
        assert parallel.tp_ok(ParallelShape(num_attention_heads=32, num_kv_heads=6), 4) is False

    def test_kv_smaller_than_tp_needs_integer_replication(self):
        # kv < tp：vLLM 复制 KV head，要求 tp 是 kv 的整数倍（heads 仍须整除）
        assert parallel.tp_ok(ParallelShape(num_attention_heads=32, num_kv_heads=2), 8) is True
        assert parallel.tp_ok(ParallelShape(num_attention_heads=30, num_kv_heads=5), 15) is True
        assert parallel.tp_ok(ParallelShape(num_attention_heads=30, num_kv_heads=5), 6) is False

    def test_mqa_kv_one_any_tp(self):
        # MQA（kv=1）：tp 多大都能复制
        assert parallel.tp_ok(ParallelShape(num_attention_heads=32, num_kv_heads=1), 16) is True


class TestRejectReason:
    def test_reason_names_both_head_counts(self):
        reason = parallel.tp_reject_reason(ParallelShape(num_attention_heads=32, num_kv_heads=6), 4)
        assert reason is not None and "KV head 数（6）" in reason

    def test_reason_suggests_legal_tp(self):
        reason = parallel.tp_reject_reason(ParallelShape(num_attention_heads=32, num_kv_heads=8), 3)
        assert reason is not None
        assert "attention head 数（32）" in reason
        # 32 的合法约数里应当出现 4/8，且不出现非法值 3
        assert "4" in reason and "8" in reason

    def test_legal_tp_has_no_reason(self):
        assert parallel.tp_reject_reason(ParallelShape(num_attention_heads=32, num_kv_heads=8), 8) is None

    def test_shape_unknown_has_no_reason(self):
        assert parallel.tp_reject_reason(ParallelShape(), 5) is None


class TestSuggested:
    def test_divisors_of_heads(self):
        assert parallel.suggested_tp_sizes(ParallelShape(num_attention_heads=12)) == [1, 2, 3, 4, 6, 12]

    def test_bounded_by_max_tp(self):
        assert parallel.suggested_tp_sizes(ParallelShape(num_attention_heads=32), max_tp=4) == [1, 2, 4]

    def test_zero_max_tp_yields_no_candidates(self):
        """上限 0（节点上一张可用的卡都没有）→ 空列表；夹成 1 会让调用方索引不存在的卡。"""
        assert parallel.suggested_tp_sizes(ParallelShape(), max_tp=0) == []

    def test_kv_constraint_narrows_candidates(self):
        # heads=32 的约数 {1,2,4,8,16,32} 里，kv=6 只放行 tp ∈ {1,2}
        # （tp=4 时 6 % 4 ≠ 0；tp≥8 时 tp 不是 6 的整数倍）
        sizes = parallel.suggested_tp_sizes(ParallelShape(num_attention_heads=32, num_kv_heads=6))
        assert sizes == [1, 2]


class TestValidateParallelShape:
    def test_homogeneous_rejects_illegal_tp(self):
        with pytest.raises(LLMPodValidationError, match="attention head 数"):
            parallel.validate_parallel_shape(Topology(tp=3), ParallelShape(num_attention_heads=32, num_kv_heads=8))

    def test_unknown_shape_passes(self):
        parallel.validate_parallel_shape(Topology(tp=7), ParallelShape())

    def test_heterogeneous_tags_offending_copy(self):
        topo = Topology(copies=[{"tp": 2}, {"tp": 3}])
        with pytest.raises(LLMPodValidationError, match="第 2 份："):
            parallel.validate_parallel_shape(topo, ParallelShape(num_attention_heads=32, num_kv_heads=8))

    def test_all_copies_legal_passes(self):
        # 异构各份各自合法（tp=2 与 tp=3 都是 12 heads / 6 kv 的合法值）
        parallel.validate_parallel_shape(
            Topology(copies=[{"tp": 2}, {"tp": 3}]), ParallelShape(num_attention_heads=12, num_kv_heads=6)
        )


class TestShapeFor:
    """形状取值的**引擎门控**：按 head 切分 tp 的引擎才有约束；llama.cpp 走 layer 切分，不受约束。"""

    class _Row:
        num_attention_heads = 28
        num_kv_heads = 4

    @staticmethod
    def _adapter(name):
        from llmpod.apps.manager.deployment.engine import get_adapter

        return get_adapter(name)

    def test_head_splitting_engine_keeps_shape(self):
        shape = parallel.shape_for(self._adapter("vllm"), self._Row())
        assert shape.num_attention_heads == 28 and shape.known is True

    def test_layer_splitting_engine_gets_empty_shape(self):
        assert parallel.shape_for(self._adapter("llamacpp"), self._Row()).known is False

    def test_layer_splitting_engine_allows_non_divisible_tp(self):
        # 28 head 放 3 张卡：按 head 口径必拒，llama.cpp 按层切分则合法
        adapter = self._adapter("llamacpp")
        shape = parallel.shape_for(adapter, self._Row())
        parallel.validate_parallel_shape(Topology(tp=3), shape)

    def test_head_splitting_engine_still_rejects_same_tp(self):
        adapter = self._adapter("vllm")
        with pytest.raises(LLMPodValidationError, match="attention head 数"):
            parallel.validate_parallel_shape(Topology(tp=3), parallel.shape_for(adapter, self._Row()))


class TestFromModel:
    def test_reads_columns(self):
        class _Row:
            num_attention_heads = 32
            num_kv_heads = 8

        shape = ParallelShape.from_model(_Row())
        assert (shape.num_attention_heads, shape.num_kv_heads) == (32, 8)
        assert shape.known is True

    def test_ignores_junk_values(self):
        class _Row:
            num_attention_heads = 0
            num_kv_heads = -1

        shape = ParallelShape.from_model(_Row())
        assert shape.known is False

    def test_missing_attributes(self):
        assert ParallelShape.from_model(object()).known is False


class TestPpRejectReason:
    """pp 的模型侧门槛：只做负向排除，判断不了就放行。"""

    def test_pp_one_never_rejected(self):
        assert parallel.pp_reject_reason(1, architecture="BertForMaskedLM", model_type="rerank") is None

    def test_encoder_architecture_rejected(self):
        reason = parallel.pp_reject_reason(2, architecture="Qwen2ForSequenceClassification", model_type=None)
        assert reason is not None and "ForSequenceClassification" in reason

    def test_non_decoder_category_rejected(self):
        reason = parallel.pp_reject_reason(2, architecture=None, model_type="embedding")
        assert reason is not None and "embedding" in reason

    def test_decoder_llm_allowed(self):
        assert parallel.pp_reject_reason(2, architecture="Qwen2ForCausalLM", model_type="llm") is None

    def test_multimodal_decoder_allowed(self):
        # 多模态解码器（如 Qwen2-VL）在引擎里支持 pp，不能按类别一刀切
        assert (
            parallel.pp_reject_reason(2, architecture="Qwen2VLForConditionalGeneration", model_type="multimodal")
            is None
        )

    def test_unknown_model_allowed(self):
        # 判不了就放行：误拒一个能跑的部署比漏放一个跑不起来的更糟
        assert parallel.pp_reject_reason(4, architecture=None, model_type=None) is None


class TestValidateParallelHook:
    def test_reports_model_reason_before_engine_capability(self):
        """模型侧原因先报：它更具体，且引擎将来开放 pp 后依然成立（引擎未声明只是「还没测」）。"""
        from llmpod.apps.manager.deployment.engine import get_adapter

        adapter = get_adapter("vllm")
        with pytest.raises(LLMPodValidationError, match="ForSequenceClassification"):
            adapter.validate_parallel(
                Topology(tp=2, pp=2), architecture="Qwen2ForSequenceClassification", model_type=None
            )

    def test_encoder_category_rejected_by_category(self):
        from llmpod.apps.manager.deployment.engine import get_adapter

        with pytest.raises(LLMPodValidationError, match="不是解码器"):
            get_adapter("vllm").validate_parallel(Topology(tp=2, pp=2), architecture=None, model_type="rerank")

    def test_decoder_falls_through_to_engine_capability(self, monkeypatch):
        """解码器 LLM 不被模型门槛拦下 → 落到引擎能力声明（vllm 已声明 pp，压掉后验证兜底拒）。"""
        from llmpod.apps.manager.deployment.engine import get_adapter
        from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter

        monkeypatch.setattr(VLLMAdapter, "supported_parallel", ["tp"])
        with pytest.raises(LLMPodValidationError, match="未声明支持流水线并行"):
            get_adapter("vllm").validate_parallel(
                Topology(tp=2, pp=2), architecture="Qwen2ForCausalLM", model_type="llm"
            )

    def test_decoder_pp_passes_when_declared(self):
        """vllm 现声明 pp：解码器 LLM 的 pp 拓扑直接放行（多机 pp 形态的主通路）。"""
        from llmpod.apps.manager.deployment.engine import get_adapter

        get_adapter("vllm").validate_parallel(Topology(tp=2, pp=2), architecture="Qwen2ForCausalLM", model_type="llm")


class TestTopologyIntegration:
    def test_validate_topology_uses_shape(self):
        from llmpod.apps.manager.deployment.scheduler.topology import validate_topology

        with pytest.raises(LLMPodValidationError, match="KV head 数（6）"):
            validate_topology(Topology(tp=4), shape=ParallelShape(num_attention_heads=32, num_kv_heads=6))

    def test_validate_topology_without_shape_unchanged(self):
        from llmpod.apps.manager.deployment.scheduler.topology import validate_topology

        # 不传 shape 时行为与历史一致（tp=4 结构上合法）
        validate_topology(Topology(tp=4))

    @pytest.mark.parametrize(
        "engine,tp",
        [("vllm", 1), ("sglang", 2)],
        ids=["vllm-tp1-pp2-nnodes2", "sglang-tp2-pp2-nnodes2"],
    )
    def test_multinode_pp_topology_all_green(self, engine, tp):
        """多机 pp 主推形态（pp=nnodes）在 vLLM/SGLang 上结构校验 + 并行能力双绿。"""
        from llmpod.apps.manager.deployment.engine import get_adapter
        from llmpod.apps.manager.deployment.scheduler.topology import validate_topology

        topo = Topology(tp=tp, pp=2, nnodes=2)
        adapter = get_adapter(engine)
        validate_topology(topo, adapter=adapter)
        adapter.validate_parallel(topo, architecture="Qwen2ForCausalLM", model_type="llm")
