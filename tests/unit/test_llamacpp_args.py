"""llamacpp 引擎适配器单测：generate_start_args / build_device_config / build_env。"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.engine.llamacpp import LlamaCppAdapter
from llmpod.apps.manager.deployment.schemas import InstanceSpec, Topology
from llmpod.core.exceptions import LLMPodValidationError


def _inst(**kw):
    base = dict(
        instance_id="i1",
        rank=0,
        role="full",
        tp=1,
        assigned_gpus=[0],
        port=8080,
        model_path="/data/m.gguf",
        served_model_name="m",
        task="generate",
    )
    base.update(kw)
    return InstanceSpec(**base)


class TestGenerateStartArgs:
    def test_llm_nvidia_full_offload(self):
        args = LlamaCppAdapter().generate_start_args(_inst(), Topology(tp=1), {})
        assert "--model" in args and "/data/m.gguf" in args
        assert "--host" in args and "0.0.0.0" in args
        assert "--port" in args and "8080" in args
        assert "--alias" in args and "m" in args
        # 有分配 GPU → 全量 offload
        i = args.index("--n-gpu-layers")
        assert args[i + 1] == "999"
        # LLM 不带 --embedding
        assert "--embedding" not in args

    def test_embedding_adds_flag(self):
        args = LlamaCppAdapter().generate_start_args(_inst(task="embed"), Topology(tp=1), {})
        assert "--embedding" in args

    def test_ctx_size_from_max_model_len(self):
        args = LlamaCppAdapter().generate_start_args(_inst(), Topology(tp=1, max_model_len=4096), {})
        i = args.index("--ctx-size")
        assert args[i + 1] == "4096"

    def test_no_ctx_when_unset(self):
        args = LlamaCppAdapter().generate_start_args(_inst(), Topology(tp=1), {})
        assert "--ctx-size" not in args

    def test_zero_ngl_without_gpus(self):
        """无分配 GPU（cpu 路径）→ -ngl 0（纯 CPU/内存）。"""
        args = LlamaCppAdapter().generate_start_args(_inst(assigned_gpus=None), Topology(tp=1), {})
        i = args.index("--n-gpu-layers")
        assert args[i + 1] == "0"

    def test_ngl_from_offload_layers(self):
        """编排器反解出的档位优先于「有卡就全量」的旧口径。"""
        args = LlamaCppAdapter().generate_start_args(_inst(assigned_gpus=[0], offload_layers=7), Topology(tp=1), {})
        i = args.index("--n-gpu-layers")
        assert args[i + 1] == "7"

    def test_cpu_lane_omits_gpu_flags(self):
        """纯 CPU 部署（gpu_arch=cpu）不下发任何 GPU 语义参数（无卡时它们没有意义）。"""
        args = LlamaCppAdapter().generate_start_args(
            _inst(assigned_gpus=[], gpu_arch="cpu", offload_layers=0),
            Topology(tp=1),
            {},
        )
        assert "--n-gpu-layers" not in args
        assert "--no-kv-offload" not in args

    def test_offload_flags_are_protected(self):
        """``--n-gpu-layers`` 放开给用户在 Step3 手改（层数系统只在两端取解）。"""
        assert "--n-gpu-layers" not in LlamaCppAdapter.protected_flags
        assert "--cache-type-k" not in LlamaCppAdapter.protected_flags

    def test_cross_node_rejected(self):
        """llama.cpp 单机引擎：nnodes>1 直接拒（无 rendezvous / NCCL）。拒绝在
        validate_topology 的 choke point（读 supports_multinode 声明），引擎不再
        自带一条覆写路径。"""
        from llmpod.apps.manager.deployment.scheduler.topology import validate_topology

        with pytest.raises(LLMPodValidationError, match="不支持多机部署"):
            validate_topology(Topology(tp=2, nnodes=2), adapter=LlamaCppAdapter())
        validate_topology(Topology(tp=2), adapter=LlamaCppAdapter())  # 单机不拒

    def test_extra_args_appended(self):
        args = LlamaCppAdapter().generate_start_args(_inst(), Topology(tp=1), {"extra_args": ["--threads", "8"]})
        assert "--threads" in args and "8" in args


class TestBuildDeviceConfig:
    def test_nvidia_returns_assigned_indices(self):
        from llmpod.apps.manager.deployment.schemas import DeviceConfig

        dc = LlamaCppAdapter.build_device_config(_inst(assigned_gpus=[0, 1]), "nvidia")
        assert isinstance(dc, DeviceConfig)
        assert dc.runtime == "nvidia"  # 显式 nvidia 运行时（裸 device_requests 在部分节点看不到卡）
        assert dc.gpus == [0, 1]
        assert dc.privileged is False

    def test_cpu_returns_no_gpu_devices(self):
        """cpu 部署 → gpus=None（不申请设备）；不能是 []（Node 侧 [] = 全部 GPU）。"""
        dc = LlamaCppAdapter.build_device_config(_inst(assigned_gpus=None), "cpu")
        assert dc.gpus is None

    def test_unknown_arch_not_implemented(self):
        with pytest.raises(NotImplementedError):
            LlamaCppAdapter.build_device_config(_inst(), "rocm")


class TestBuildEnv:
    def test_hf_offline(self):
        from llmpod.apps.manager.deployment.schemas import EngineConfig

        env = LlamaCppAdapter.build_env(_inst(), Topology(tp=1), EngineConfig())
        assert env.get("HF_HUB_OFFLINE") == "1"


class TestDeployable:
    def test_deployable_true(self):
        assert LlamaCppAdapter.deployable is True

    def test_catalog_nvidia_cpu_images(self):
        """catalog 是 llama.cpp 镜像唯一定义处：nvidia→server-cuda13（驱动未知取 default），cpu→server。"""
        from llmpod.apps.manager.deployment import image_catalog

        image_catalog.reload()
        assert image_catalog.pick_image(engine="llamacpp", arch="nvidia") == "ghcr.io/ggml-org/llama.cpp:server-cuda13"
        assert image_catalog.pick_image(engine="llamacpp", arch="cpu") == "ghcr.io/ggml-org/llama.cpp:server"

    def test_category_validation(self):
        # GGUF LLM / embedding 受支持；rerank 不支持
        LlamaCppAdapter.validate_category("llm")
        LlamaCppAdapter.validate_category("embedding")
        with pytest.raises(LLMPodValidationError):
            LlamaCppAdapter.validate_category("rerank")
