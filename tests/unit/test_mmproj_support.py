"""GGUF 多模态（mmproj 视觉投影器）支持：探测选型 / 下发清单 / 完整性校验 / 启动参数。

覆盖链路：``inference.pick_mmproj``（F16 精度优先）→ ``Model.mmproj_file`` → 节点
``_allow_patterns``/``_verify_existing``（配套文件一起下发并校验）→ ``InstanceSpec.mmproj_path``
→ 适配器 ``--mmproj``。
"""

from __future__ import annotations

from llmpod.apps.manager.deployment.engine.llamacpp import LlamaCppAdapter
from llmpod.apps.manager.deployment.placement import recipe_digest
from llmpod.apps.manager.deployment.schemas import InstanceSpec, Topology
from llmpod.apps.manager.model_source import inference
from llmpod.apps.node.executors._hf import _allow_patterns
from llmpod.apps.node.executors.ensure_model import _verify_existing
from llmpod.config.enums import ModelType


class TestPickMmproj:
    def test_prefers_f16_over_bf16(self):
        names = ["model-Q4_K_M.gguf", "mmproj-BF16.gguf", "mmproj-F16.gguf"]
        assert inference.pick_mmproj(names) == "mmproj-F16.gguf"

    def test_falls_back_to_bf16_then_any_gguf(self):
        assert inference.pick_mmproj(["mmproj-BF16.gguf"]) == "mmproj-BF16.gguf"
        assert inference.pick_mmproj(["mmproj-UD-Q8_0.gguf"]) == "mmproj-UD-Q8_0.gguf"

    def test_none_without_mmproj_or_non_gguf(self):
        assert inference.pick_mmproj(["model.gguf"]) is None
        assert inference.pick_mmproj([]) is None
        assert inference.pick_mmproj(None) is None

    def test_non_gguf_extension_mmproj_recognized(self):
        """``*.mmproj`` / 无扩展名等非 .gguf 命名也识别（llama.cpp 只看文件内容）。"""
        assert inference.pick_mmproj(["model.gguf", "model.mmproj"]) == "model.mmproj"
        assert inference.pick_mmproj(["model.gguf", "mmproj-f16"]) == "mmproj-f16"

    def test_other_format_weights_never_selected(self):
        """他格式投影器权重（.pt/.safetensors/.json…）不可被 --mmproj 加载，绝不选中；
        全仓库只有他格式时返回 None（部署退化为纯文本服务，不崩溃循环）。"""
        assert inference.pick_mmproj(["model.gguf", "mmproj.pt"]) is None
        assert inference.pick_mmproj(["model.gguf", "mmproj-f16.safetensors"]) is None
        assert inference.pick_mmproj(["model.gguf", "mmproj.json"]) is None
        assert inference.pick_mmproj(["mmproj-f16.pt", "mmproj-model-f16.gguf"]) == "mmproj-model-f16.gguf"


class TestAllowPatterns:
    def test_extra_files_merged_into_patterns(self):
        assert _allow_patterns("v/model.gguf", ["mmproj-F16.gguf"]) == [
            "v/model.gguf",
            "v/model.gguf/**",
            "mmproj-F16.gguf",
        ]

    def test_no_subpath_stays_full_repo(self):
        """无 subpath → 全量（extras 不能当唯一白名单，否则只下一个 mmproj）。"""
        assert _allow_patterns(None, ["mmproj-F16.gguf"]) is None
        assert _allow_patterns(None, []) is None
        assert _allow_patterns("", ["mmproj-F16.gguf"]) is None


class TestRecipeDigest:
    def test_backward_compatible_without_extras(self):
        """无 mmproj 的模型 digest 与旧版逐字节一致（已下载目录不失效）。"""
        base = recipe_digest("huggingface", "org/m", "m.gguf", "main")
        assert base == recipe_digest("huggingface", "org/m", "m.gguf", "main", extra_files=[])
        assert base == recipe_digest("huggingface", "org/m", "m.gguf", "main", extra_files=None)

    def test_extra_files_change_digest(self):
        base = recipe_digest("huggingface", "org/m", "m.gguf", "main")
        with_mmproj = recipe_digest("huggingface", "org/m", "m.gguf", "main", extra_files=["mmproj-F16.gguf"])
        assert base != with_mmproj


class TestVerifyExtras:
    def test_missing_extra_means_not_ready(self, tmp_path):
        (tmp_path / "model.gguf").write_text("")
        assert _verify_existing(tmp_path, "single_gguf", None) == str(tmp_path / "model.gguf")
        assert _verify_existing(tmp_path, "single_gguf", None, ["mmproj-F16.gguf"]) is None

        (tmp_path / "mmproj-F16.gguf").write_text("")
        assert _verify_existing(tmp_path, "single_gguf", None, ["mmproj-F16.gguf"]) == str(tmp_path / "model.gguf")

    def test_mmproj_not_counted_as_second_model(self, tmp_path):
        """single_gguf 判定排除 mmproj（配套件，不是第二个主模型）。"""
        (tmp_path / "model.gguf").write_text("")
        (tmp_path / "mmproj-F16.gguf").write_text("")
        assert _verify_existing(tmp_path, "single_gguf", None) == str(tmp_path / "model.gguf")


def _inst(**kw) -> InstanceSpec:
    base = dict(
        instance_id="i1",
        rank=0,
        role="full",
        tp=1,
        assigned_gpus=[0],
        port=8080,
        model_path="/data/models/m.gguf",
        served_model_name="m",
        task="generate",
    )
    base.update(kw)
    return InstanceSpec(**base)


class TestAdapterMultimodal:
    def test_multimodal_declared_and_maps_to_generate(self):
        assert ModelType.MULTIMODAL in LlamaCppAdapter.supported_categories
        assert LlamaCppAdapter.task_for(ModelType.MULTIMODAL) == "generate"

    def test_mmproj_injected(self):
        args = LlamaCppAdapter().generate_start_args(
            _inst(mmproj_path="/data/models/mmproj-F16.gguf"), Topology(tp=1), {}
        )
        i = args.index("--mmproj")
        assert args[i + 1] == "/data/models/mmproj-F16.gguf"

    def test_no_mmproj_flag_without_path(self):
        args = LlamaCppAdapter().generate_start_args(_inst(), Topology(tp=1), {})
        assert "--mmproj" not in args

    def test_mmproj_also_injected_for_cpu_lane(self):
        """纯 CPU 部署也带 --mmproj（视觉能力与上不上卡无关）。"""
        args = LlamaCppAdapter().generate_start_args(
            _inst(
                assigned_gpus=[],
                gpu_arch="cpu",
                offload_layers=0,
                mmproj_path="/data/models/mmproj-F16.gguf",
            ),
            Topology(tp=1),
            {},
        )
        assert "--mmproj" in args
        assert "--n-gpu-layers" not in args
