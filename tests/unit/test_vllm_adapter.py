"""vLLM 适配器单测。"""

from __future__ import annotations

import json

import pytest

from llmpod.apps.manager.deployment import image_catalog
from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.schemas import EngineConfig, InstanceSpec, Topology
from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter
from llmpod.config.enums import ModelType
from llmpod.core.exceptions import LLMPodValidationError


@pytest.fixture
def adapter() -> VLLMAdapter:
    # get_adapter 注入 engine_name（词表查找键的唯一定义处）；裸实例化该键为空串，
    # 词表门控用例会静默退回最新词表
    return get_adapter("vllm")


# 「未声明 pp 的引擎」共用 stub（vllm 现已声明，压掉声明模拟占位/未测引擎）
class _PPUndeclared(VLLMAdapter):
    supported_parallel = ["tp"]


class TestGenerateStartArgs:
    def test_single_node(self, adapter):
        """nnodes=1：无 --nnodes/--headless，纯 TP。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/models/Qwen", served_model_name="qwen", port=8000, nnodes=1, tp=2),
            Topology(tp=2),
            {},
        )
        assert "--model" in args and "/models/Qwen" in args
        assert "--served-model-name" in args and "qwen" in args
        assert "--tensor-parallel-size" in args and "2" in args
        assert "--nnodes" not in args
        assert "--headless" not in args

    def test_task_generate_no_flag(self, adapter):
        """task=generate（默认）：无池化 flag / 无 tool-calling 之外的注入（回归守卫：防重新引入已弃用的 --task）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            {},
        )
        assert "--task" not in args
        assert "--runner" not in args
        assert "--convert" not in args

    def test_task_embed(self, adapter):
        """task=embed（embedding 模型）→ --runner pooling --convert embed → /v1/embeddings。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="embed"),
            Topology(tp=1),
            {},
        )
        assert args[args.index("--runner") + 1] == "pooling"
        assert args[args.index("--convert") + 1] == "embed"

    def test_task_classify_rerank(self, adapter):
        """task=classify（rerank 模型，VLLMAdapter.type_to_task['rerank']='classify'）→ --runner pooling --convert classify → /v1/rerank。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="classify"),
            Topology(tp=1),
            {},
        )
        assert args[args.index("--runner") + 1] == "pooling"
        assert args[args.index("--convert") + 1] == "classify"

    def test_task_classify_not_overridden_by_user(self, adapter):
        """用户 extra_args 里的 --runner/--convert 不能覆盖系统必控（fill_absent_args）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="classify"),
            Topology(tp=1),
            {"extra_args": ["--convert", "embed"]},
        )
        assert args[args.index("--convert") + 1] == "classify"

    def test_task_classify_injects_hf_overrides(self, adapter):
        """task=classify（rerank）须带 --hf-overrides 在线转换头参数。

        真机根因（vLLM 0.27.1 源码核实）：ForCausalLM 架构的 reranker（如
        Qwen3-Reranker）config 无 num_labels（transformers 默认补 2），经
        ``--runner pooling --convert classify`` 转成 seq-cls 后有两道门槛：
        1) ``enable_scoring_api`` 要求 classify ``num_labels == 1`` 才注册
           /rerank、/score 路由——缺省 2 时只起 /classify、/pooling，rerank 404；
        2) 权重里没有分类头（1_LogitScore 只有 true/false token id），需
           ``method=from_2_way_softmax + classifier_from_token`` 让 vLLM 在线
           用 true/false token 的 lm_head 差构造 score 头（adapters.py
           SequenceClassificationConfig 同一约定）。
        """
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="classify"),
            Topology(tp=1),
            {},
        )
        i = args.index("--hf-overrides")
        overrides = json.loads(args[i + 1])
        assert overrides["num_labels"] == 1
        assert overrides["method"] == "from_2_way_softmax"
        assert len(overrides["classifier_from_token"]) == 2

    def test_task_classify_hf_overrides_protected(self, adapter):
        """用户 extra_args 的 --hf-overrides 不能覆盖系统注入的 num_labels=1。

        --hf-overrides 属系统必控（注入即保护语义同 --convert；用户若带同名
        flag 会被 fill_absent_args 丢弃，防止抹掉 num_labels 后 rerank 404）。
        """
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="classify"),
            Topology(tp=1),
            {"extra_args": ["--hf-overrides", '{"dtype": "float32"}']},
        )
        assert args.count("--hf-overrides") == 1
        assert json.loads(args[args.index("--hf-overrides") + 1])["num_labels"] == 1

    def test_task_embed_no_hf_overrides(self, adapter):
        """task=embed 不注入 --hf-overrides（embed 的 scoring API 无 num_labels 门槛）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="embed"),
            Topology(tp=1),
            {},
        )
        assert "--hf-overrides" not in args

    def test_multi_node_rank0(self, adapter):
        """nnodes=2, node_rank=0：加 --nnodes/--node-rank 0/--master-addr，无 --headless。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=2,
                node_rank=0,
                master_addr="10.0.1.5",
                master_port=29501,
            ),
            Topology(tp=2),
            {},
        )
        assert "--nnodes" in args and "2" in args
        assert "--node-rank" in args and "0" in args
        assert "--master-addr" in args and "10.0.1.5" in args
        assert "--master-port" in args and "29501" in args
        assert "--headless" not in args  # rank-0 起 API server

    def test_multi_node_worker_headless(self, adapter):
        """nnodes=2, node_rank=1（worker）：加 --headless。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=2,
                node_rank=1,
                master_addr="10.0.1.5",
                master_port=29501,
            ),
            Topology(tp=2),
            {},
        )
        assert "--headless" in args
        assert "--node-rank" in args and "1" in args

    def test_user_extra_args_appended(self, adapter):
        """用户 extra_args 追加，不覆盖系统必控项。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            {"extra_args": ["--max-model-len", "8192", "--trust-remote-code"]},
        )
        assert "--max-model-len" in args and "8192" in args
        assert "--trust-remote-code" in args

    def test_served_model_name_defaults_to_path(self, adapter):
        """无 served_model_name 时回退到 model_path（不留空）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/models/Qwen", port=8000, nnodes=1),
            Topology(tp=1),
            {},
        )
        sm_idx = args.index("--served-model-name")
        assert args[sm_idx + 1] == "/models/Qwen"

    def test_max_model_len_from_topology(self, adapter):
        """Topology.max_model_len 非空时发 --max-model-len。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", port=8000, nnodes=1),
            Topology(tp=1, max_model_len=16384),
            {},
        )
        assert "--max-model-len" in args
        assert args[args.index("--max-model-len") + 1] == "16384"

    def test_max_model_len_none_omitted(self, adapter):
        """Topology.max_model_len 为 None 时不发 --max-model-len（让 vLLM 用默认）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", port=8000, nnodes=1),
            Topology(tp=1, max_model_len=None),
            {},
        )
        assert "--max-model-len" not in args

    def test_embed_skips_max_model_len(self, adapter):
        """task=embed（池化模型）：不发 --max-model-len（config 自带 max_position，硬传会超限拒启）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", port=8000, nnodes=1, task="embed"),
            Topology(tp=1, max_model_len=8192),
            {},
        )
        assert "--max-model-len" not in args

    def test_ascend_pooling_args(self, adapter):
        """cann 路径（_ascend_args）：task=classify → --runner pooling --convert classify，无 --max-model-len。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=1,
                task="classify",
                gpu_arch="cann",
            ),
            Topology(tp=1, max_model_len=8192),
            {},
        )
        assert args[:2] == ["vllm", "serve"]
        assert args[args.index("--runner") + 1] == "pooling"
        assert args[args.index("--convert") + 1] == "classify"
        assert "--max-model-len" not in args

    def test_ascend_generate_ctx_fallback(self, adapter):
        """cann 路径：generate 任务显式 --max-model-len（缺省兜底 4096 防 310P OOM）。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=1,
                task="generate",
                gpu_arch="cann",
            ),
            Topology(tp=1, max_model_len=None),
            {},
        )
        assert args[args.index("--max-model-len") + 1] == "4096"

    def test_ascend_tp_injected(self, adapter):
        """cann 多 NPU：注入 --tensor-parallel-size（此前静默丢 tp，调度分 N 卡引擎只跑 1 张）。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=1,
                task="generate",
                gpu_arch="cann",
                tp=2,
            ),
            Topology(tp=2, max_model_len=None),
            {},
        )
        assert args[args.index("--tensor-parallel-size") + 1] == "2"

    def test_ascend_single_gpu_no_tp_flag(self, adapter):
        """cann 单卡：不注入 --tensor-parallel-size（单 NPU 无需 flag，行为与历史一致）。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=1,
                task="generate",
                gpu_arch="cann",
            ),
            Topology(tp=1, max_model_len=None),
            {},
        )
        assert "--tensor-parallel-size" not in args

    def test_ascend_pp_rejected(self, adapter):
        """cann × pp>1 硬拒（vllm-ascend PP 不成熟且无验证环境；静默丢 flag 会口径分裂）。"""
        with pytest.raises(LLMPodValidationError, match="pp"):
            adapter.generate_start_args(
                InstanceSpec(
                    model_path="/m",
                    served_model_name="m",
                    port=8000,
                    nnodes=1,
                    task="generate",
                    gpu_arch="cann",
                    pp=2,
                ),
                Topology(tp=1, pp=2, max_model_len=None),
                {},
            )

    def test_ascend_pp1_not_rejected_by_sibling_pp2(self, adapter):
        """cann 实例自己 pp=1 → 兄弟份用 pp>1 不连带拒（逐实例判，不判整拓扑）。

        异构副本 copies=[{tp:1,pp:1}, {tp:1,pp:2}] 时部署级 topology 含 pp=2 份，
        但落到 cann 上的那份自己 pp=1 应照常出参。
        """
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=1,
                task="generate",
                gpu_arch="cann",
                pp=1,
            ),
            Topology(copies=[{"tp": 1, "pp": 1}, {"tp": 1, "pp": 2}], max_model_len=None),
            {},
        )
        assert "vllm" in args and "serve" in args  # 正常产出启动参数

    def test_ascend_extra_args_no_override(self, adapter):
        """cann 用户 extra_args 走补缺式追加：系统 flag 同名被跳过（防用户覆盖 tp/端口）。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=1,
                task="generate",
                gpu_arch="cann",
                tp=2,
            ),
            Topology(tp=2, max_model_len=None),
            {"extra_args": ["--tensor-parallel-size", "1", "--port", "9999", "--trust-remote-code"]},
        )
        assert args[args.index("--tensor-parallel-size") + 1] == "2"  # 系统值保留
        assert args[args.index("--port") + 1] == "8000"
        assert "--trust-remote-code" in args  # 非系统 flag 原样追加

    def test_pp_declared_and_allowed(self, adapter):
        """pp 已声明（多机 pp 形态放行）→ validate_parallel 对 pp=2 放行。"""
        from llmpod.apps.manager.deployment.schemas import Topology as Topology_

        assert "pp" in VLLMAdapter.supported_parallel
        VLLMAdapter.validate_parallel(Topology_(tp=1, pp=2))

    def test_pp_undeclared_rejected(self):
        """未声明 pp 的引擎（stub 模拟占位/未测引擎）pp>1 → validate_parallel 拒。"""
        from llmpod.apps.manager.deployment.schemas import Topology as Topology_

        with pytest.raises(LLMPodValidationError, match="未声明支持流水线并行"):
            _PPUndeclared.validate_parallel(Topology_(tp=1, pp=2))

    def test_validate_parallel_hetero_error_names_copy(self):
        """异构副本中未声明的份被点名（第 N 份），人读定位（压掉 pp 声明模拟未测引擎）。"""
        from llmpod.apps.manager.deployment.schemas import Topology as Topology_

        with pytest.raises(LLMPodValidationError, match="第 2 份"):
            _PPUndeclared.validate_parallel(Topology_(copies=[{"tp": 1}, {"tp": 1, "pp": 2}]))

    def test_pipeline_parallel_size(self, adapter):
        """pp=2 → --pipeline-parallel-size 2（PP 不再被静默忽略）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, pp=2),
            Topology(tp=1, pp=2),
            {},
        )
        assert "--pipeline-parallel-size" in args
        assert args[args.index("--pipeline-parallel-size") + 1] == "2"

    def test_instance_parallel_wins_over_topology(self, adapter):
        """并行度以实例自带值为准：异构副本各份形状不同，部署级 topology 只等于首份。

        用 topology 拼 flag 会让第二份按首份卡数启动（vLLM 只用部分卡或 world size
        与卡数不符直接失败）。
        """
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, tp=2, pp=1),
            Topology(tp=1, pp=1),  # 首份形状：tp=1
            {},
        )
        assert args[args.index("--tensor-parallel-size") + 1] == "2"
        assert "--pipeline-parallel-size" not in args

    def test_pp_default_omitted(self, adapter):
        """pp=1（默认）→ 不发 --pipeline-parallel-size。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1, pp=1),
            {},
        )
        assert "--pipeline-parallel-size" not in args


class TestToolCalling:
    """默认启用 tool calling（仅 generate 任务；parser 可被用户覆盖）。"""

    def test_enabled_default_for_generate(self, adapter):
        """task=generate（LLM/MULTIMODAL chat）→ 注入 --enable-auto-tool-choice + 兜底 parser。

        parser 与开关必须成对出现——vLLM 对「开了 auto-tool-choice 却没给 parser」是启动期直接拒。
        架构未知（探测不到）的模型走通用兜底，且**不**注入推理解析器。
        """
        instance = InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1)
        args = adapter.assemble_args(instance, Topology(tp=1), None, {})
        assert "--enable-auto-tool-choice" in args
        idx = args.index("--tool-call-parser")
        assert args[idx + 1] == adapter.default_tool_call_parser == "hermes"
        assert "--reasoning-parser" not in args

    def test_vllm_066_uses_only_its_verified_cli_vocabulary(self, adapter):
        instance = InstanceSpec(
            model_path="/m",
            served_model_name="m",
            port=8000,
            nnodes=1,
            model_architecture="Qwen2ForCausalLM",
        )
        args = adapter.generate_start_args(
            instance,
            Topology(tp=1),
            {},
            engine_version="0.6.6",
        )
        assert "--enable-auto-tool-choice" in args
        assert args[args.index("--tool-call-parser") + 1] == "hermes"
        assert "--reasoning-parser" not in args

        embed_args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="embed"),
            Topology(tp=1),
            {},
            engine_version="0.6.6",
        )
        assert embed_args[embed_args.index("--task") + 1] == "embed"

    def test_qwen3_gets_family_parsers(self, adapter):
        """Qwen3 系：工具走自己的 XML 协议，且必须注入推理解析器（否则思考混进正文）。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=1,
                model_architecture="Qwen3ForCausalLM",
            ),
            Topology(tp=1),
            {},
        )
        assert args[args.index("--tool-call-parser") + 1] == "qwen3_coder"
        assert args[args.index("--reasoning-parser") + 1] == "qwen3"

    def test_non_qwen_family_gets_no_reasoning_parser(self, adapter):
        """非 Qwen3 家族不注入推理解析器。

        这不是保守——vLLM 的 qwen3 语法机起始即 reasoning 态，给不吐 ``<think>`` 的模型
        塞它会让整段正文被判成 reasoning、``content`` 返回 null（空回复）。
        """
        for arch in ("LlamaForCausalLM", "MistralForCausalLM", "DeepseekV3ForCausalLM"):
            args = adapter.generate_start_args(
                InstanceSpec(
                    model_path="/m",
                    served_model_name="m",
                    port=8000,
                    nnodes=1,
                    model_architecture=arch,
                ),
                Topology(tp=1),
                {},
            )
            assert "--reasoning-parser" not in args, arch
            assert args[args.index("--tool-call-parser") + 1] == "hermes", arch

    def test_qwen3_moe_and_vl_also_matched(self, adapter):
        """架构前缀匹配：MoE / VL 变体也认（同一家族的解析器）。"""
        for arch in ("Qwen3MoeForCausalLM", "Qwen3VLForConditionalGeneration"):
            tool, reasoning = adapter.parsers_for(arch)
            assert (tool, reasoning) == ("qwen3_coder", "qwen3"), arch

    def test_parsers_for_unknown_architecture(self, adapter):
        assert adapter.parsers_for(None) == ("hermes", None)
        assert adapter.parsers_for("") == ("hermes", None)

    def test_user_extra_args_override_family_default_parsers(self, adapter):
        """用户给了 --tool-call-parser/--reasoning-parser → 平台不再注入默认值。

        同一架构名下 JSON/XML 两种工具调用协议都存在（如 Qwen3 系的不同量化分支），
        协议对不对只有用户知道自己拿的模型——家族默认只是兜底，用户的值必须原样生效
        （base 先注入会把用户同名 flag 当重复丢掉）。
        """
        instance = InstanceSpec(
            model_path="/m",
            served_model_name="m",
            port=8000,
            nnodes=1,
            model_architecture="Qwen3ForCausalLM",
        )
        params = {"extra_args": ["--tool-call-parser", "hermes"]}
        args = adapter.generate_start_args(instance, Topology(tp=1), params)
        # 默认 qwen3_coder 不出现，用户值唯一生效；成对开关不重复注入
        assert args[args.index("--tool-call-parser") + 1] == "hermes"
        assert args.count("--tool-call-parser") == 1
        assert "qwen3_coder" not in args
        assert "--enable-auto-tool-choice" in args  # 用户没给开关时平台补上（vLLM 要求成对）
        # 推理解析器未被用户覆盖 → 家族默认照常注入
        assert args[args.index("--reasoning-parser") + 1] == "qwen3"

        params2 = {"extra_args": ["--tool-call-parser", "hermes", "--reasoning-parser", "deepseek_r1"]}
        args2 = adapter.generate_start_args(instance, Topology(tp=1), params2)
        # 两个解析器都由用户给出 → 家族默认一个都不注入，用户的值原样生效
        assert args2[args2.index("--reasoning-parser") + 1] == "deepseek_r1"
        assert "qwen3" not in args2 and "qwen3_coder" not in args2

    def test_parser_flags_offered_as_suggestions(self, adapter):
        """两个解析器 flag 出现在参数建议里，且带常用值列表。

        取值随模型家族而异、平台已给默认值，但列一份常用值让人能手调——尤其
        --reasoning-parser，此前它根本不出现在任何地方，用户想设都不知道有这个名字。
        """
        by_name = {s.name: s for s in adapter.param_suggestions}
        assert {"--tool-call-parser", "--reasoning-parser"} <= set(by_name)

        tool, reasoning = by_name["--tool-call-parser"], by_name["--reasoning-parser"]
        # 常用值非空（前端据此展开候选项；空表会退化成只有 flag 名）
        assert tool.choices and reasoning.choices
        # 兜底值必须在候选里，否则「默认值不在列表」会让用户困惑
        assert tool.default == adapter.default_tool_call_parser in tool.choices
        # 家族专属值在位：Qwen3 是本次要照顾的正主
        assert {"qwen3_coder", "deepseek_v3", "glm45"} <= set(tool.choices)
        assert {"qwen3", "deepseek_r1"} <= set(reasoning.choices)

    def test_absent_for_embed(self, adapter):
        """task=embed（embedding 模型）→ 不注入 tool calling（无 chat 工具端点）。"""
        instance = InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="embed")
        args = adapter.assemble_args(instance, Topology(tp=1), None, {})
        assert "--enable-auto-tool-choice" not in args
        assert "--tool-call-parser" not in args

    def test_absent_for_classify(self, adapter):
        """task=classify（rerank 模型）→ 不注入 tool calling。"""
        instance = InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="classify")
        args = adapter.assemble_args(instance, Topology(tp=1), None, {})
        assert "--enable-auto-tool-choice" not in args

    def test_user_override_parser_via_assemble_args(self, adapter):
        """--tool-call-parser 非 protected：用户 extra_args 经 assemble_args 可覆盖默认值。"""
        args = adapter.assemble_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            None,
            {"extra_args": ["--tool-call-parser", "deepseek_v3"]},
        )
        assert "--enable-auto-tool-choice" in args  # 默认仍开启
        idx = args.index("--tool-call-parser")
        assert args[idx + 1] == "deepseek_v3"


class TestParserVersionGating:
    """家族解析器 / 池化词表按镜像 engine_version 查 ``engine_versions`` 词表分档。

    旧镜像的 vLLM 没有这些名字（registry 里查无此名 → 启动期 KeyError 拒启；
    runner/convert flag 未引入 → argparse 拒启），注入前查 ``inference_images.json``
    的版本词表，查无此名逐项降档。词表断代按 vLLM 各 tag 源码核实（2026-09）：
    qwen3_coder 0.10.0 起、qwen3 推理解析器 0.9.0 起、--runner/--convert 0.11.0 起
    （更早版本 --task embed/classify 全程在册）。
    """

    def test_qwen3_below_tool_floor(self, adapter):
        """0.9.0（catalog cu128 条目）：qwen3_coder 未注册 → 工具回落 hermes；qwen3 推理解析器已在册照常注入。"""
        tool, reasoning = adapter.parsers_for("Qwen3ForCausalLM", "0.9.0")
        assert (tool, reasoning) == ("hermes", "qwen3")

    def test_qwen3_below_reasoning_floor(self, adapter):
        """0.8.5/0.7.3（catalog cu124/cu121 条目）：连 qwen3 推理解析器都没有 → 全回落通用兜底。"""
        for v in ("0.8.5", "0.7.3"):
            assert adapter.parsers_for("Qwen3ForCausalLM", v) == ("hermes", None), v

    def test_qwen3_at_floor(self, adapter):
        """词表收录的现代版本：两个家族解析器都在册，完整注入。"""
        for v in ("0.22.1", "0.23.0", "0.30.0"):
            assert adapter.parsers_for("Qwen3ForCausalLM", v) == ("qwen3_coder", "qwen3"), v

    def test_unknown_version_treated_as_latest(self, adapter):
        """版本未知（自带镜像 / 未标版本条目）或词表未收录 → 按最新词表——维持既有行为。"""
        assert adapter.parsers_for("Qwen3ForCausalLM", None) == ("qwen3_coder", "qwen3")
        assert adapter.parsers_for("Qwen3ForCausalLM", "") == ("qwen3_coder", "qwen3")
        # 词表未收录的版本号：缺数据不降级（与自带镜像同姿态）
        assert adapter.parsers_for("Qwen3ForCausalLM", "0.99.0") == ("qwen3_coder", "qwen3")

    def test_vocab_rejects_all_parsers(self, adapter, monkeypatch):
        """空词表（该版本明确一个解析器都不认）≠ 未收录：全降档。"""
        monkeypatch.setattr(
            image_catalog,
            "_load_vocabs",
            lambda: {("vllm", "0.99.9"): image_catalog.EngineVocab(tool_parsers=[], reasoning_parsers=[])},
        )
        assert adapter.parsers_for("Qwen3ForCausalLM", "0.99.9") == ("hermes", None)

    def test_old_image_qwen3_starts_with_hermes(self, adapter):
        """端到端：0.9.0 镜像 + Qwen3 架构 → 启动参数用 hermes（该镜像起得来）。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=1,
                model_architecture="Qwen3ForCausalLM",
            ),
            Topology(tp=1),
            {},
            engine_version="0.9.0",
        )
        assert args[args.index("--tool-call-parser") + 1] == "hermes"
        assert args[args.index("--reasoning-parser") + 1] == "qwen3"

    def test_pooling_old_vocabulary(self, adapter):
        """0.11 前镜像：embed/classify 走旧词表 --task（runner/convert 会被 argparse 拒启）。"""
        for task in ("embed", "classify"):
            args = adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task=task),
                Topology(tp=1),
                {},
                engine_version="0.9.0",
            )
            assert args[args.index("--task") + 1] == task, task
            assert "--runner" not in args and "--convert" not in args, task
            # classify 的 --hf-overrides 是新版适配器协议，旧词表路径不带
            if task == "classify":
                assert "--hf-overrides" not in args

    def test_pooling_new_vocabulary_kept(self, adapter):
        """新词表档（≥0.11 及版本未知）：维持 --runner pooling --convert 词表。"""
        for v in ("0.23.0", "0.30.0", None):
            args = adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="embed"),
                Topology(tp=1),
                {},
                engine_version=v,
            )
            assert args[args.index("--runner") + 1] == "pooling", v
            assert args[args.index("--convert") + 1] == "embed", v

    def test_pooling_gating_via_assemble_args(self, adapter):
        """assemble_args 从 catalog 条目取 engine_version（走 entry 而非手传）。"""
        entry = image_catalog.ImageEntry(id="t", engine="vllm", arch="nvidia", engine_version="0.9.0", image="x")
        args = adapter.assemble_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="embed"),
            Topology(tp=1),
            entry,
            {},
        )
        assert args[args.index("--task") + 1] == "embed"

    def test_catalog_vocab_covers_all_versions(self, adapter):
        """对账：catalog images 里每个标了 engine_version 的条目都有词表段（漏收录=静默按最新词表）。"""
        image_catalog.reload()
        missing = sorted(
            {
                f"{e.engine}:{e.engine_version}"
                for e in image_catalog.list_entries()
                if e.engine_version and image_catalog.engine_vocab(e.engine, e.engine_version) is None
            }
        )
        assert missing == [], f"engine_versions 词表漏收录: {missing}"

    def test_family_parsers_present_in_latest_vocab(self, adapter):
        """对账：_FAMILY_PARSERS 引用的名字在词表收录的最近版本里都在册（上游改名/删除该同步两处）。"""
        image_catalog.reload()
        latest = image_catalog.engine_vocab("vllm", "0.30.0")
        for _, tool, reasoning in adapter._FAMILY_PARSERS:
            assert tool in latest.tool_parsers
            assert reasoning in latest.reasoning_parsers

    def test_multinode_rejected_on_old_vocab(self, adapter):
        """旧版本镜像（词表 multinode=false）多机部署 → 组装期硬拒。

        elastic 五件套 0.12 才进 vLLM（0.9.0 的 --headless 是 DP 语义，同名不同义；
        0.7.3 连 flag 都没有），旧镜像多机只有 Ray 路径——平台未实现，静默拼
        不认的 flag 只会在容器阶段崩，拒绝比跑错强（与 Ascend pp>1 同构）。
        """
        with pytest.raises(LLMPodValidationError, match="多机"):
            adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=2, node_rank=0),
                Topology(tp=2, nnodes=2),
                {},
                engine_version="0.9.0",
            )
        with pytest.raises(LLMPodValidationError, match="多机"):
            adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=2, node_rank=0),
                Topology(tp=2, nnodes=2),
                {},
                engine_version="0.6.6",
            )

    def test_multinode_ok_on_modern_vocab(self, adapter):
        """现代版本（及未收录/未知版本）：多机五件套照常注入。"""
        for v in ("0.23.0", "0.30.0", None):
            args = adapter.generate_start_args(
                InstanceSpec(
                    model_path="/m",
                    served_model_name="m",
                    port=8000,
                    nnodes=2,
                    node_rank=1,
                    master_addr="10.0.0.1",
                    master_port=29501,
                ),
                Topology(tp=2, nnodes=2),
                {},
                engine_version=v,
            )
            assert "--nnodes" in args and "--master-addr" in args, v
            assert "--headless" in args, v  # worker(rank>0) 不起 API server


class TestEstimateVramGb:
    """estimate_vram_gb：per-gpu = (权重 + KV(context)) / tp，与 feasibility 同源。"""

    def test_weight_only_no_context(self, adapter):
        # 无 context → KV=0 → per-gpu = weight/tp
        assert adapter.estimate_vram_gb(10.0, Topology(tp=1)) == 10.0

    def test_weight_plus_kv(self, adapter):
        # 10GB + ctx 8192 → required 11，per-gpu(tp=1) = 11
        assert adapter.estimate_vram_gb(10.0, Topology(tp=1, max_model_len=8192)) == 11.0

    def test_tp_divides(self, adapter):
        # tp=2 → per-gpu = 11/2 = 5.5
        assert adapter.estimate_vram_gb(10.0, Topology(tp=2, max_model_len=8192)) == 5.5

    def test_pp_divides(self, adapter):
        # tp=2, pp=2 → per-gpu = 11/(2×2) = 2.75
        assert adapter.estimate_vram_gb(10.0, Topology(tp=2, pp=2, max_model_len=8192)) == 2.75

    def test_missing_size_raises(self, adapter):
        from llmpod.core.exceptions import LLMPodValidationError

        with pytest.raises(LLMPodValidationError):
            adapter.estimate_vram_gb(None, Topology(tp=1))

    def test_pooling_model_type_skips_kv(self, adapter):
        """model_type=embedding（无 KV cache 类别）→ KV=0，只算权重。"""
        assert adapter.estimate_vram_gb(10.0, Topology(tp=1, max_model_len=8192), model_type="embedding") == 10.0

    def test_rerank_model_type_skips_kv(self, adapter):
        """model_type=rerank（无 KV cache 类别）→ KV=0，只算权重。"""
        assert adapter.estimate_vram_gb(10.0, Topology(tp=1, max_model_len=8192), model_type="rerank") == 10.0

    def test_generate_model_type_keeps_kv(self, adapter):
        """model_type=llm（生成类别）→ KV 照算，与不传 model_type 的旧口径一致。"""
        assert adapter.estimate_vram_gb(10.0, Topology(tp=1, max_model_len=8192), model_type="llm") == 11.0


class TestBuildDeviceConfig:
    def test_nvidia(self):
        dc = VLLMAdapter.build_device_config(InstanceSpec(assigned_gpus=[0, 1]), "nvidia")
        assert dc.runtime == "nvidia"  # 显式 nvidia 运行时（裸 device_requests 在部分节点看不到卡）
        assert dc.gpus == [0, 1]
        assert dc.driver_mounts == []
        assert dc.privileged is False

    def test_nvidia_no_gpus_defaults_all(self):
        dc = VLLMAdapter.build_device_config(InstanceSpec(assigned_gpus=[]), "nvidia")
        assert dc.gpus == []  # 空表 = 全部 GPU（Node count=-1）

    def test_cann_device_config(self):
        """cann（Ascend）：build_device_config 返回 ascend runtime + davinci 设备，gpus=None。"""
        dc = VLLMAdapter.build_device_config(InstanceSpec(assigned_gpus=[0]), "cann")
        assert dc.runtime == "ascend"
        assert dc.gpus is None  # NPU 走 runtime+extra_devices，不走 nvidia device_requests
        assert any("/dev/davinci0" in d for d in dc.extra_devices)


class TestBuildEnv:
    def test_hf_endpoint_default(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=1), Topology(), EngineConfig())
        assert env["HF_ENDPOINT"] == "https://hf-mirror.com"

    def test_hf_endpoint_override(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=1), Topology(), EngineConfig(hf_endpoint="https://modelscope.cn"))
        assert env["HF_ENDPOINT"] == "https://modelscope.cn"

    def test_nccl_multinode(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=2), Topology(), EngineConfig())
        assert "NCCL_SOCKET_IFNAME" in env
        assert env["NCCL_DEBUG"] == "INFO"

    def test_gloo_ifname_multinode(self, adapter):
        # Gloo（CPU 集合通信组）与 NCCL 同网卡注入；门槛是 nnodes>1，与 pp 数值无关。
        # 无节点上报也无用户配置时回落适配器兜底 eth0（NCCL 侧带 = 前缀强制精确匹配）
        env = adapter.build_env(InstanceSpec(nnodes=3, pp=6), Topology(), EngineConfig())
        assert env["GLOO_SOCKET_IFNAME"] == "eth0"
        assert env["NCCL_SOCKET_IFNAME"] == "=eth0"

    def test_node_reported_ifname_used(self, adapter):
        # 节点上报的网卡名（持有 Node.ip）作为系统注入值：NCCL 带 = 前缀，
        # Gloo 裸名（gloo 不认任何修饰语法）
        env = adapter.build_env(InstanceSpec(nnodes=2, node_ifname="enp3s0"), Topology(), EngineConfig())
        assert env["NCCL_SOCKET_IFNAME"] == "=enp3s0"
        assert env["GLOO_SOCKET_IFNAME"] == "enp3s0"

    def test_explicit_nccl_ifname_wins_and_passthrough(self, adapter):
        # 用户显式配置最高优先级且原样透传（可能自带 ^ 排除等 NCCL 语法，不加 = 前缀）
        env = adapter.build_env(
            InstanceSpec(nnodes=2, node_ifname="enp3s0"), Topology(), EngineConfig(nccl_ifname="^lo,docker0")
        )
        assert env["NCCL_SOCKET_IFNAME"] == "^lo,docker0"
        assert env["GLOO_SOCKET_IFNAME"] == "^lo,docker0"

    def test_nccl_absent_single_node(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=1), Topology(), EngineConfig())
        assert "NCCL_SOCKET_IFNAME" not in env
        assert "GLOO_SOCKET_IFNAME" not in env

    def test_engine_ready_timeout(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=1), Topology(), EngineConfig(), health_timeout_s=900)
        assert env["VLLM_ENGINE_READY_TIMEOUT_S"] == "900"

    def test_cann_visible_devices_all_assigned(self, adapter):
        """cann 多卡：ASCEND_VISIBLE_DEVICES 曝全部分配的 NPU（与 device_config 挂载同口径）。

        只曝第一张会让 vllm-ascend 可见设备数 < tp，多卡初始化必败。
        """
        env = adapter.build_env(
            InstanceSpec(nnodes=1, gpu_arch="cann", assigned_gpus=[1, 3]),
            Topology(tp=2),
            EngineConfig(),
        )
        assert env["ASCEND_VISIBLE_DEVICES"] == "1,3"

    def test_cann_visible_devices_default_single(self, adapter):
        """cann 无 assigned_gpus（旧数据/单卡缺省）：回落第一张，行为与历史一致。"""
        env = adapter.build_env(
            InstanceSpec(nnodes=1, gpu_arch="cann"),
            Topology(),
            EngineConfig(),
        )
        assert env["ASCEND_VISIBLE_DEVICES"] == "0"


class TestHealthCheck:
    def test_default_health_path(self):
        path, pred = VLLMAdapter.health_check()
        assert path == "/health"


class TestTaskFor:
    def test_vllm_task_vocabulary(self):
        """vLLM：rerank→classify（cross-encoder），embedding→embed，generate 默认。"""
        assert VLLMAdapter.task_for("llm") == "generate"
        assert VLLMAdapter.task_for("multimodal") == "generate"
        assert VLLMAdapter.task_for("embedding") == "embed"
        assert VLLMAdapter.task_for("rerank") == "classify"
        assert VLLMAdapter.task_for("unknown_type") == "generate"  # 未声明 → generate

    def test_category_for_task_reverse_lookup(self):
        """category_for_task：task→model_type 反查（type_to_task 首个命中）；非本引擎词汇 → None。"""
        assert VLLMAdapter.category_for_task("embed") == ModelType.EMBEDDING
        assert VLLMAdapter.category_for_task("classify") == ModelType.RERANK
        assert VLLMAdapter.category_for_task("generate") in (ModelType.LLM, ModelType.MULTIMODAL)
        assert VLLMAdapter.category_for_task("not-a-task") is None

    def test_has_kv_cache_is_category_level(self):
        """has_kv_cache 为类别级判定（ModelType 唯一定义处），各引擎共用。"""
        assert not ModelType.has_kv_cache(ModelType.EMBEDDING)
        assert not ModelType.has_kv_cache(ModelType.RERANK)
        assert ModelType.has_kv_cache(ModelType.LLM)
        assert ModelType.has_kv_cache(None)  # 未知类别保守按有 KV 估

        from llmpod.apps.manager.deployment.engine.sglang import SGLangAdapter
        from llmpod.apps.manager.deployment.engine.llamacpp import LlamaCppAdapter

        # SGLang rerank task 词与 vLLM 不同（rerank vs classify），但反查回同一类别
        assert SGLangAdapter.category_for_task("rerank") == ModelType.RERANK
        assert LlamaCppAdapter.category_for_task("embed") == ModelType.EMBEDDING

    def test_sglang_task_vocabulary_differs(self):
        """SGLang 词汇与 vLLM 不同：rerank 就是 rerank（不是 classify）。"""
        from llmpod.apps.manager.deployment.engine.sglang import SGLangAdapter

        assert SGLangAdapter.task_for("rerank") == "rerank"
        assert SGLangAdapter.task_for("embedding") == "embed"

    def test_llamacpp_no_rerank(self):
        """llama.cpp 无 rerank（supported_categories 也无），task 兜底 generate。"""
        from llmpod.apps.manager.deployment.engine.llamacpp import LlamaCppAdapter

        assert LlamaCppAdapter.task_for("llm") == "generate"
        assert LlamaCppAdapter.task_for("embedding") == "embed"
        assert LlamaCppAdapter.task_for("rerank") == "generate"  # 不支持，validate_category 会先拒
