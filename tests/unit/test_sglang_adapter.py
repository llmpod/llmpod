"""SGLang 适配器单测。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment import image_catalog
from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.schemas import EngineConfig, InstanceSpec, Topology
from llmpod.apps.manager.deployment.engine.sglang import SGLangAdapter
from llmpod.core.exceptions import LLMPodValidationError


@pytest.fixture
def adapter() -> SGLangAdapter:
    # get_adapter 注入 engine_name（词表查找键的唯一定义处），裸实例化该键为空串
    return get_adapter("sglang")


class TestGenerateStartArgs:
    def test_single_node(self, adapter):
        """nnodes=1：无 --nnodes/--dist-init-addr，纯 TP + 基础 flag。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/models/Qwen", served_model_name="qwen", port=8000, nnodes=1, tp=2),
            Topology(tp=2),
            {},
        )
        assert "--model-path" in args and "/models/Qwen" in args
        assert "--served-model-name" in args and "qwen" in args
        assert "--host" in args and "0.0.0.0" in args
        assert "--port" in args and "8000" in args
        assert "--tp-size" in args and "2" in args
        assert "--nnodes" not in args
        assert "--dist-init-addr" not in args

    def test_task_generate_no_embedding_flag(self, adapter):
        """task=generate（默认）：不传 --is-embedding（SGLang 默认生成模式）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            {},
        )
        assert "--is-embedding" not in args


class TestAutoParsers:
    """解析器交给引擎自检：``auto`` 按模型 chat template 判定，判不出只禁用不报错。

    这让我们不必维护「家族 → 解析器名」映射，也避开各后端词表不同（SGLang 的
    ``qwen25`` / ``deepseek-r1`` vs vLLM 的 ``qwen3_coder`` / ``deepseek_r1``）。
    """

    def test_generate_injects_auto_both(self, adapter):
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            {},
        )
        assert args[args.index("--tool-call-parser") + 1] == "auto"
        assert args[args.index("--reasoning-parser") + 1] == "auto"

    def test_auto_requires_recent_version(self, adapter):
        """``auto`` 是 0.5.12 才进 choices 的（0.4.x/0.5.11 传 auto 被 argparse 拒启）。

        旧镜像两个 flag 都不注入：解析器缺失只是思考混正文/工具不可用，
        起不来才是事故。档位由 ``inference_images.json`` 的 ``engine_versions``
        词表记录（parser_auto），代码只查表。
        """
        for v in ("0.3.0", "0.4.2", "0.4.6", "0.4.7", "0.4.10", "0.5.11"):
            args = adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
                Topology(tp=1),
                {},
                engine_version=v,
            )
            assert "--tool-call-parser" not in args, v
            assert "--reasoning-parser" not in args, v

    def test_sglang_0300_skips_flags_missing_from_legacy_cli(self, adapter):
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            {},
            engine_version="0.3.0",
        )
        for flag in ("--enable-metrics", "--tool-call-parser", "--reasoning-parser", "--pp-size"):
            assert flag not in args
        assert "--tp-size" in args and "--mem-fraction-static" in args

    def test_auto_injected_at_and_after_floor(self, adapter):
        for v in ("0.5.17", None):
            args = adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
                Topology(tp=1),
                {},
                engine_version=v,
            )
            assert args[args.index("--tool-call-parser") + 1] == "auto", v
            assert args[args.index("--reasoning-parser") + 1] == "auto", v

    def test_auto_vocab_unlisted_version_treated_latest(self, adapter):
        """词表未收录的版本号：缺数据不降级（与自带镜像同姿态，按最新词表注入）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            {},
            engine_version="9.9.9",
        )
        assert args[args.index("--tool-call-parser") + 1] == "auto"

    def test_auto_gating_via_assemble_args(self, adapter):
        """assemble_args 从 catalog 条目取 engine_version（catalog 里 0.4.x 条目面向 Turing/老驱动）。"""
        entry = image_catalog.ImageEntry(id="t", engine="sglang", arch="nvidia", engine_version="0.4.10", image="x")
        args = adapter.assemble_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            entry,
            {},
        )
        assert "--tool-call-parser" not in args
        assert "--reasoning-parser" not in args

    def test_embed_does_not_inject(self, adapter):
        """embedding/rerank 无 chat 端点，不注入（与 vLLM 侧同口径）。

        注意任务名各引擎不同：SGLang 的 rerank 任务叫 ``rerank``（vLLM 是 ``classify``），
        故这里按 SGLang 自己的词表取。
        """
        for task in ("embed", "rerank"):
            args = adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task=task),
                Topology(tp=1),
                {},
            )
            assert "--tool-call-parser" not in args, task
            assert "--reasoning-parser" not in args, task

    def test_parser_flags_offered_as_suggestions(self, adapter):
        """两个 flag 出现在参数建议里，且带 SGLang 自己的词表（与 vLLM 不同名）。"""
        by_name = {s.name: s for s in adapter.param_suggestions}
        assert {"--tool-call-parser", "--reasoning-parser"} <= set(by_name)
        tool, reasoning = by_name["--tool-call-parser"], by_name["--reasoning-parser"]
        assert tool.choices and reasoning.choices
        # SGLang 特有词表：qwen25 / deepseek-r1（连字符）——vLLM 侧没有这两个名字
        assert "qwen25" in tool.choices
        assert "deepseek-r1" in reasoning.choices

    def test_user_override_wins(self, adapter):
        """非 protected：用户 extra_args 可覆盖 auto。"""
        args = adapter.assemble_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            None,
            {"extra_args": ["--reasoning-parser", "qwen3"]},
        )
        assert args[args.index("--reasoning-parser") + 1] == "qwen3"

    def test_task_embed_embedding_flag(self, adapter):
        """task=embed（embedding 模型）→ --is-embedding → /v1/embeddings。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="embed"),
            Topology(tp=1),
            {},
        )
        assert "--is-embedding" in args

    def test_task_rerank_embedding_flag(self, adapter):
        """task=rerank（cross-encoder）→ 同样走 --is-embedding（SGLang 用 embedding 服务器暴露 /v1/rerank）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, task="rerank"),
            Topology(tp=1),
            {},
        )
        assert "--is-embedding" in args

    def test_multi_node_dist_init(self, adapter):
        """nnodes=2：加 --nnodes/--node-rank/--dist-init-addr（非 vLLM master-addr/port）。"""
        args = adapter.generate_start_args(
            InstanceSpec(
                model_path="/m",
                served_model_name="m",
                port=8000,
                nnodes=2,
                node_rank=1,
                master_addr="10.0.1.5",
                master_port=5001,
            ),
            Topology(tp=2),
            {},
        )
        assert "--nnodes" in args and "2" in args
        assert "--node-rank" in args and "1" in args
        assert "--dist-init-addr" in args and "10.0.1.5:5001" in args
        assert "--master-addr" not in args  # SGLang 词汇不同于 vLLM

    def test_user_extra_args_appended(self, adapter):
        """用户 extra_args 追加，不覆盖系统必控项。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1),
            {"extra_args": ["--trust-remote-code", "--dtype", "half"]},
        )
        assert "--trust-remote-code" in args
        assert "--dtype" in args and "half" in args

    def test_served_model_name_defaults_to_path(self, adapter):
        """无 served_model_name 时回退到 model_path（不留空）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/models/Qwen", port=8000, nnodes=1),
            Topology(tp=1),
            {},
        )
        sm_idx = args.index("--served-model-name")
        assert args[sm_idx + 1] == "/models/Qwen"

    def test_context_length_from_topology(self, adapter):
        """Topology.max_model_len 非空时发 --context-length。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", port=8000, nnodes=1),
            Topology(tp=1, max_model_len=16384),
            {},
        )
        assert "--context-length" in args
        assert args[args.index("--context-length") + 1] == "16384"

    def test_context_length_none_omitted(self, adapter):
        """Topology.max_model_len 为 None 时不发 --context-length。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", port=8000, nnodes=1),
            Topology(tp=1, max_model_len=None),
            {},
        )
        assert "--context-length" not in args

    def test_vram_percent_injects_mem_fraction_static(self, adapter):
        """vram_percent → --mem-fraction-static（等价 vLLM --gpu-memory-utilization）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", port=8000, nnodes=1, vram_percent=0.5),
            Topology(tp=1),
            {},
        )
        assert "--mem-fraction-static" in args
        assert args[args.index("--mem-fraction-static") + 1] == "0.5"

    def test_pp_size(self, adapter):
        """pp=2 → --pp-size 2（PP 不再被静默忽略）。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, pp=2),
            Topology(tp=1, pp=2),
            {},
        )
        assert "--pp-size" in args
        assert args[args.index("--pp-size") + 1] == "2"

    def test_pp_default_omitted(self, adapter):
        """pp=1（默认）→ 不发 --pp-size。"""
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1),
            Topology(tp=1, pp=1),
            {},
        )
        assert "--pp-size" not in args


class TestValidateCrossNode:
    """多机整除钩子：按 SGLang ``_calculate_rank_ranges`` 的真实分区规则收紧——
    总并行度整除 nnodes 不够，分区还不能有取整失真（否则引擎静默按另一个
    每节点卡数拉起，与调度预留对不上）。"""

    @pytest.mark.parametrize(
        "tp,pp,nnodes",
        [
            (1, 2, 2),  # 纯 PP 跨机：每节点 1 卡，pp 承担跨机维度
            (2, 2, 2),  # 混合：每节点 2 卡，节点内 TP + 跨机 PP
            (8, 4, 4),  # 官方样例（DeepSeek 形态，每节点 8 卡）
            (1, 4, 2),  # pp 整除 nnodes：每节点多个 stage（2 卡）
            (4, 1, 2),  # nnodes 整除 pp 且 tp 整除 nnodes/pp：一个 stage 跨两节点
        ],
        ids=["pp-only", "tp-pp-mixed", "official", "multi-stage-per-node", "stage-spans-nodes"],
    )
    def test_partitionable_shapes_pass(self, tp, pp, nnodes):
        """能按引擎分区规则均分的形状 → 放行。"""
        SGLangAdapter.validate_cross_node(tp=tp, pp=pp, nnodes=nnodes)

    def test_divisible_but_unpartitionable_rejected(self):
        """总卡数整除但引擎分不开的形状 → 拒（分区取整失真，引擎静默错配卡数）：
        tp=3/pp=2/nnodes=3（引擎每节点拉 3 进程、平台预留 2）、
        tp=2/pp=3/nnodes=2（引擎拉 2、预留 3，最后一个 stage 永不启动挂 rendezvous）、
        tp=2/pp=6/nnodes=4 同类。"""
        for tp, pp, nnodes in [(3, 2, 3), (2, 3, 2), (2, 6, 4), (3, 1, 2)]:
            with pytest.raises(LLMPodValidationError, match="均分"):
                SGLangAdapter.validate_cross_node(tp=tp, pp=pp, nnodes=nnodes)

    def test_pp_declared(self):
        """supported_parallel 声明 pp（多机 pp 形态放行）→ validate_parallel 对 pp=2 放行。"""
        assert "pp" in SGLangAdapter.supported_parallel
        SGLangAdapter.validate_parallel(Topology(tp=1, pp=2), architecture="Qwen2ForCausalLM", model_type="llm")

    def test_pp_rejected_on_old_versions(self, adapter):
        """词表 pp=false 的旧镜像（0.4.2/0.4.6 无 --pp-size）pp>1 → 组装期硬拒，不烧下载；
        版本未收录（None）→ 按最新词表，不降档。"""
        with pytest.raises(LLMPodValidationError, match="0.4.2 不支持流水线并行"):
            adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, pp=2),
                Topology(tp=1, pp=2),
                {},
                engine_version="0.4.2",
            )
        with pytest.raises(LLMPodValidationError, match="0.3.0 不支持流水线并行"):
            adapter.generate_start_args(
                InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, pp=2),
                Topology(tp=1, pp=2),
                {},
                engine_version="0.3.0",
            )
        args = adapter.generate_start_args(
            InstanceSpec(model_path="/m", served_model_name="m", port=8000, nnodes=1, pp=2),
            Topology(tp=1, pp=2),
            {},
            engine_version=None,
        )
        assert "--pp-size" in args

    def test_supported_parallel_at_version_aware(self, adapter):
        """supported_parallel_at：0.4.2 剔 pp（词表判无）、未收录版本全保留——inspect
        的 pp 门控与本拒同源。"""
        assert "pp" not in adapter.supported_parallel_at("0.4.2")
        assert "pp" not in adapter.supported_parallel_at("0.3.0")
        assert adapter.supported_parallel_at(None) == ["tp", "pp"]

    def test_legacy_multinode_flags_are_rejected(self, adapter):
        with pytest.raises(LLMPodValidationError, match="0.3.0 不支持平台的多机部署参数"):
            adapter.generate_start_args(
                InstanceSpec(
                    model_path="/m",
                    served_model_name="m",
                    port=8000,
                    nnodes=2,
                    node_rank=0,
                    master_addr="10.0.0.1",
                    master_port=5001,
                ),
                Topology(tp=2, nnodes=2),
                {},
                engine_version="0.3.0",
            )


class TestBuildDeviceConfig:
    def test_nvidia(self):
        dc = SGLangAdapter.build_device_config(InstanceSpec(assigned_gpus=[0, 1]), "nvidia")
        assert dc.runtime == "nvidia"  # 显式 nvidia 运行时（裸 device_requests 在部分节点看不到卡）
        assert dc.gpus == [0, 1]
        assert dc.driver_mounts == []
        assert dc.privileged is False

    def test_nvidia_no_gpus_defaults_all(self):
        dc = SGLangAdapter.build_device_config(InstanceSpec(assigned_gpus=[]), "nvidia")
        assert dc.gpus == []  # 空表 = 全部 GPU（Node count=-1）

    def test_cann_not_supported(self):
        with pytest.raises(NotImplementedError):
            SGLangAdapter.build_device_config(InstanceSpec(assigned_gpus=[0]), "cann")


class TestBuildEnv:
    def test_hf_endpoint_default(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=1), Topology(), EngineConfig())
        assert env["HF_ENDPOINT"] == "https://hf-mirror.com"
        assert env["HF_HUB_OFFLINE"] == "1"

    def test_hf_endpoint_override(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=1), Topology(), EngineConfig(hf_endpoint="https://modelscope.cn"))
        assert env["HF_ENDPOINT"] == "https://modelscope.cn"

    def test_nccl_multinode(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=2), Topology(), EngineConfig())
        assert "NCCL_SOCKET_IFNAME" in env
        assert "GLOO_SOCKET_IFNAME" in env
        assert env["NCCL_DEBUG"] == "INFO"
        assert env["NCCL_SOCKET_IFNAME"] == "=eth0"  # 系统注入带 = 前缀强制精确匹配

    def test_node_reported_ifname_used(self, adapter):
        # 节点上报的网卡名作系统注入值：NCCL 带 = 前缀，Gloo 裸名
        env = adapter.build_env(InstanceSpec(nnodes=2, node_ifname="enp3s0"), Topology(), EngineConfig())
        assert env["NCCL_SOCKET_IFNAME"] == "=enp3s0"
        assert env["GLOO_SOCKET_IFNAME"] == "enp3s0"

    def test_explicit_nccl_ifname_wins_and_passthrough(self, adapter):
        # 用户显式配置最高优先级且原样透传（可能自带 ^ 排除语法，不加 = 前缀）
        env = adapter.build_env(
            InstanceSpec(nnodes=2, node_ifname="enp3s0"), Topology(), EngineConfig(nccl_ifname="bond0")
        )
        assert env["NCCL_SOCKET_IFNAME"] == "bond0"
        assert env["GLOO_SOCKET_IFNAME"] == "bond0"

    def test_nccl_absent_single_node(self, adapter):
        env = adapter.build_env(InstanceSpec(nnodes=1), Topology(), EngineConfig())
        assert "NCCL_SOCKET_IFNAME" not in env
        assert "GLOO_SOCKET_IFNAME" not in env


class TestDeclarations:
    def test_deployable(self):
        assert SGLangAdapter.deployable is True

    def test_entrypoint_explicit(self):
        """SGLang 镜像无 ENTRYPOINT（CMD /bin/bash），必须显式传 launch_server。"""
        assert SGLangAdapter.entrypoint == ["python3", "-m", "sglang.launch_server"]

    def test_protected_flags_cover_hardcoded(self):
        """protected_flags 必须覆盖 generate_start_args 硬编码的系统必控 flag（防漂移）。"""
        hardcoded = {
            "--model-path",
            "--served-model-name",
            "--host",
            "--port",
            "--tp-size",
            "--pp-size",
            "--context-length",
            "--is-embedding",
            "--nnodes",
            "--node-rank",
            "--dist-init-addr",
        }
        assert hardcoded <= set(SGLangAdapter.protected_flags)

    def test_suggestions_are_not_protected(self):
        suggestion_names = {p.name for p in SGLangAdapter.param_suggestions}
        assert suggestion_names.isdisjoint(set(SGLangAdapter.protected_flags))

    def test_task_vocabulary(self):
        assert SGLangAdapter.task_for("llm") == "generate"
        assert SGLangAdapter.task_for("multimodal") == "generate"
        assert SGLangAdapter.task_for("embedding") == "embed"
        assert SGLangAdapter.task_for("rerank") == "rerank"  # 非 vLLM 的 classify
        assert SGLangAdapter.task_for("unknown_type") == "generate"

    def test_health_check_default(self):
        path, _pred = SGLangAdapter.health_check()
        assert path == "/health"


class TestCatalogResolution:
    def test_catalog_has_sglang_nvidia_entry(self):
        image_catalog.reload()
        # SGLang 0.5.x requires SM80+ and a known driver lane.
        entry = image_catalog.pick_entry(
            engine="sglang", arch="nvidia", gpu_model="NVIDIA A100-80G", driver_version="595.84"
        )
        assert entry is not None
        assert entry.image == "lmsysorg/sglang:v0.5.17"

    def test_no_sglang_ascend_entry(self):
        """SGLang 只声明 nvidia，镜像目录里不该有 sglang+cann 条目。

        曾经录过 sglang-ascend-910b/910c 两条：adapter 的 cann 设备配置未实现，条目在
        管理端镜像列表里正常展示、选了要到部署才被拒。声明与数据对齐后删除
        （目录与引擎能力的一致性由 test_image_catalog.TestCatalogConsistency 兜底）。
        """
        image_catalog.reload()
        assert "cann" not in SGLangAdapter.supported_gpu_archs
        assert image_catalog.pick_entry(engine="sglang", arch="cann", gpu_model="Ascend 910B") is None
        assert image_catalog.pick_entry(engine="sglang", arch="cann", gpu_model="Atlas A3 910C") is None
