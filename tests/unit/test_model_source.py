"""模型源识别纯函数 + 注册表单测（全离线，无网络）。

覆盖 inference.py 的提取（format/量化/MoE/架构/结构/layout）、TASK_TO_TYPE（type 建议），
build_model_files 装配，以及 get_inspector 注册表与 parse_hf_ref。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.model_source import ModelFiles, get_inspector, supported_sources
from llmpod.apps.manager.model_source.gguf_meta import GGUFMetadata
from llmpod.apps.manager.model_source.huggingface import HFHubInspector, build_model_files, parse_hf_ref
from llmpod.apps.manager.model_source.oss import OSSInspector
from llmpod.apps.manager.model_source.local import LocalInspector
from llmpod.apps.manager.model_source import inference
from llmpod.core.exceptions import LLMPodValidationError


# ============ infer_format ============


class TestInferFormat:
    def test_safetensors_preferred_over_bin(self):
        assert inference.infer_format(["model.safetensors", "pytorch_model.bin"]) == "safetensors"

    def test_gguf(self):
        assert inference.infer_format(["qwen2.5-0.5b-q4_k_m.gguf"]) == "gguf"

    def test_pytorch(self):
        assert inference.infer_format(["pytorch_model.bin", "config.json"]) == "pytorch"

    def test_onnx(self):
        assert inference.infer_format(["model.onnx"]) == "onnx"

    def test_unknown(self):
        assert inference.infer_format(["config.json", "tokenizer.json"]) is None


# ============ infer_quant ============


class TestInferQuant:
    def test_config_quant_method(self):
        cfg = {"quantization_config": {"quant_method": "AWQ"}}
        assert inference.infer_quant(cfg, ["model.safetensors"]) == "awq"

    def test_filename_awq(self):
        assert inference.infer_quant({}, ["model-awq-4bit.safetensors"]) == "awq"

    def test_filename_gptq(self):
        assert inference.infer_quant({}, ["model-gptq-4bit.safetensors"]) == "gptq"

    def test_gguf_qcode(self):
        assert inference.infer_quant({}, ["qwen2.5-0.5b-q4_k_m.gguf"]) == "q4_k_m"

    def test_bnb_alias(self):
        assert inference.infer_quant({}, ["model-bnb-4bit.safetensors"]) == "bitsandbytes"

    def test_none(self):
        assert inference.infer_quant({}, ["model.safetensors"]) is None

    def test_mmproj_does_not_supply_the_quant(self):
        """mmproj 是视觉投影器（另一份权重，精度常写 F16），不能当成主模型的量化。"""
        assert inference.infer_quant({}, ["model.gguf", "mmproj-model-f16.gguf"]) is None
        # 主模型自带标记时也不该被 mmproj 抢走
        assert inference.infer_quant({}, ["model-Q4_K_M.gguf", "mmproj-model-f32.gguf"]) == "q4_k_m"

    def test_non_weight_files_are_not_scanned(self):
        """tokenizer/vocab 之类也带 f16/f32 字样，扫进来会盖过真正的权重名。"""
        assert inference.infer_quant({}, ["tokenizer-f32768.json", "model-Q8_0.gguf"]) == "q8_0"
        assert inference.infer_quant({}, ["vocab-f16.json", "model-Q8_0.gguf"]) == "q8_0"

    def test_hash_fragments_do_not_impersonate_a_quant(self):
        """散列名里凑巧含 f16 不该被认成量化（``0af164d3`` 里就有）。"""
        assert inference.infer_quant({}, ["model-0af164d3.gguf"]) is None
        # 下划线是合法分隔符，不能被同一条定界规则误伤
        assert inference.infer_quant({}, ["model_q8_0.gguf"]) == "q8_0"

    def test_bf16_not_read_as_f16(self):
        """``bf16`` 里含子串 ``f16``——两个词不并列写出来就会报成 f16。"""
        assert inference.infer_quant({}, ["foo-bf16.gguf"]) == "bf16"
        assert inference.infer_quant({}, ["foo-f16.gguf"]) == "f16"


class TestInferQuantGGUFHeader:
    """GGUF 走文件头：``general.file_type`` 描述的才是这批权重的实际量化。

    文件名可能带根本不是量化类型的营销词（``Q5_K_L``——llama.cpp 的 ftype 里没有它），
    也可能干脆不带标记（``deepseek-r1-14b.gguf``），两者都靠不住。
    """

    def test_header_beats_filename(self):
        # 文件名说 q8_0，文件头说 15=Q4_K_M → 以文件头为准
        meta = GGUFMetadata(file_type=15)
        assert inference.infer_quant({}, ["model-Q8_0.gguf"], meta) == "q4_k_m"

    def test_header_fills_in_when_filename_silent(self):
        assert inference.infer_quant({}, ["model.gguf"], GGUFMetadata(file_type=30)) == "iq4_xs"

    def test_falls_back_to_filename_without_header(self):
        assert inference.infer_quant({}, ["model-Q8_0.gguf"], None) == "q8_0"

    def test_falls_back_to_filename_when_header_type_unknown(self):
        # file_type 存在但编号认不出（已移除 / 未知）→ 别丢掉文件名里的线索
        assert inference.infer_quant({}, ["model-Q8_0.gguf"], GGUFMetadata(file_type=9999)) == "q8_0"

    def test_config_still_wins(self):
        cfg = {"quantization_config": {"quant_method": "AWQ"}}
        assert inference.infer_quant(cfg, ["model-Q8_0.gguf"], GGUFMetadata(file_type=15)) == "awq"


# ============ infer_moe ============


class TestInferMoE:
    def test_top_level(self):
        assert inference.infer_moe({"num_experts": 8}) is True

    def test_nested_text_config(self):
        assert inference.infer_moe({"text_config": {"num_local_experts": 4}}) is True

    def test_not_moe(self):
        assert inference.infer_moe({"num_hidden_layers": 24}) is False


# ============ infer_architecture（身份原样提取） ============


class TestInferArchitecture:
    def test_first_architecture(self):
        cfg = {"architectures": ["Qwen2ForCausalLM"]}
        assert inference.infer_architecture(cfg) == "Qwen2ForCausalLM"

    def test_model_type_fallback(self):
        assert inference.infer_architecture({"model_type": "qwen2"}) == "qwen2"

    def test_format_fallback_custom(self):
        assert (
            inference.infer_architecture({"format": "inflect_v2_inference_config_v1"})
            == "inflect_v2_inference_config_v1"
        )

    def test_diffusers_model_index(self):
        assert inference.infer_architecture({}, {"_class_name": "FluxPipeline"}) == "FluxPipeline"

    def test_none(self):
        assert inference.infer_architecture({}) is None


# ============ type_from_pipeline_tag（HF tag → 建议 type） ============


class TestTypeFromPipelineTag:
    @pytest.mark.parametrize(
        "tag, expected",
        [
            ("text-generation", "llm"),
            ("text2text-generation", "llm"),
            ("image-text-to-text", "multimodal"),
            ("image-to-text", "multimodal"),
            ("any-to-any", "multimodal"),
            ("feature-extraction", "embedding"),
            ("sentence-similarity", "embedding"),
            ("image-feature-extraction", "embedding"),
            ("text-ranking", "rerank"),
            ("automatic-speech-recognition", "stt"),
            ("text-to-speech", "tts"),
            ("text-to-audio", "tts"),
            ("text-to-image", "image"),
        ],
    )
    def test_mapped(self, tag, expected):
        assert inference.type_from_pipeline_tag(tag) == expected

    @pytest.mark.parametrize(
        "tag", ["text-classification", "fill-mask", "object-detection", "question-answering", "translation", ""]
    )
    def test_unmapped_returns_none(self, tag):
        # 映射不了的 / 空 → None（用户选，不硬塞）
        assert inference.type_from_pipeline_tag(tag) is None

    def test_none_returns_none(self):
        assert inference.type_from_pipeline_tag(None) is None


# ============ detect_layout（文件布局） ============


class TestDetectLayout:
    def test_transformers(self):
        assert inference.detect_layout(["config.json", "model.safetensors"]) == "transformers"

    def test_diffusers(self):
        assert inference.detect_layout(["model_index.json", "flux1-dev.safetensors", "vae/config.json"]) == "diffusers"

    def test_gguf(self):
        assert inference.detect_layout(["model-q4_k_m.gguf"]) == "gguf"

    def test_onnx(self):
        assert inference.detect_layout(["model.onnx"]) == "onnx"

    def test_custom_config(self):
        # 有 config 但非 transformers（无 safetensors），如 inflect/vits
        assert inference.detect_layout(["config.json", "model.pth"]) == "custom_config"

    def test_custom_script(self):
        assert inference.detect_layout(["inference.py", "model.pth"]) == "custom_script"

    def test_unknown(self):
        assert inference.detect_layout(["README.md"]) == "unknown"


# ============ sum_weight_gb ============


class TestSumWeightGb:
    def test_sum(self):
        sibs = [
            {"rfilename": "model-00001-of-00002.safetensors", "size": 5 * 1024**3},
            {"rfilename": "model-00002-of-00002.safetensors", "size": 3 * 1024**3},
            {"rfilename": "config.json", "size": 1024},
        ]
        assert inference.sum_weight_gb(sibs) == 8.0

    def test_no_weights(self):
        assert inference.sum_weight_gb([{"rfilename": "config.json", "size": 1024}]) is None

    def test_missing_size_skipped_not_zero(self):
        # per-file size 缺失 → 跳过该文件，不把 0 当 found 累加 → None（非 0.0）
        sibs = [{"rfilename": "model.safetensors", "size": None}, {"rfilename": "other.safetensors"}]
        # 全部缺 size → 无 found → None
        assert inference.sum_weight_gb(sibs) is None

    def test_partial_missing_size_sums_known(self):
        # 部分 size 已知 → 只累加已知者，缺失的不当 0
        sibs = [
            {"rfilename": "model-00001-of-00002.safetensors", "size": 5 * 1024**3},
            {"rfilename": "model-00002-of-00002.safetensors", "size": None},
        ]
        assert inference.sum_weight_gb(sibs) == 5.0


# ============ detect_source_kind（结构形态） ============


class TestDetectSourceKind:
    def test_gguf_single_file(self):
        assert inference.detect_source_kind([{"rfilename": "model-q4_k_m.gguf"}]) == "single_gguf"

    def test_gguf_shards(self):
        sibs = [
            {"rfilename": "model-00001-of-00002.gguf"},
            {"rfilename": "model-00002-of-00002.gguf"},
        ]
        assert inference.detect_source_kind(sibs) == "gguf_shards"

    def test_transformers_folder(self):
        sibs = [{"rfilename": "config.json"}, {"rfilename": "model.safetensors"}]
        assert inference.detect_source_kind(sibs) == "transformers_folder"

    def test_diffusers_folder(self):
        sibs = [{"rfilename": "model_index.json"}, {"rfilename": "flux1-dev.safetensors"}]
        assert inference.detect_source_kind(sibs) == "diffusers_folder"

    def test_gguf_with_config_is_transformers_folder(self):
        # gguf + config.json → config 优先，非 single_gguf（single_gguf 需无 config）
        sibs = [{"rfilename": "config.json"}, {"rfilename": "model.gguf"}]
        assert inference.detect_source_kind(sibs) == "transformers_folder"

    def test_unknown(self):
        assert inference.detect_source_kind([{"rfilename": "README.md"}]) == "unknown"


class TestStructureFromSourceKind:
    def test_gguf_kinds_are_single_file(self):
        assert inference.structure_from_source_kind("single_gguf") == "single_file"
        assert inference.structure_from_source_kind("gguf_shards") == "single_file"

    def test_folder_kinds_are_directory(self):
        for k in ("transformers_folder", "diffusers_folder", "unknown", None):
            assert inference.structure_from_source_kind(k) == "directory"


# ============ build_model_files（真实结构装配） ============


class TestBuildModelFiles:
    def test_qwen_transformers(self):
        # 真实 Qwen2.5：transformers 布局 + text-generation tag → suggested_type=llm
        siblings = [
            {"rfilename": "config.json", "size": 1024},
            {"rfilename": "model.safetensors", "size": 1024**3},
        ]
        config = {"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"}
        mf = build_model_files(siblings, "text-generation", config=config)
        assert mf.layout == "transformers"
        assert mf.source_kind == "transformers_folder"
        assert mf.format == "safetensors"
        assert mf.architecture == "Qwen2ForCausalLM"
        assert mf.suggested_type == "llm"
        assert mf.has_config is True

    def test_mmproj_detected_from_full_repo_when_subpath_narrowed(self):
        """用户贴具体变体文件（siblings 收窄到那一个文件）时，mmproj 仍按仓库全量清单探到。

        真实形态：unsloth/Qwen3.8-27B-GGUF —— 变体 GGUF 与 mmproj-*.gguf 并列在仓库根目录，
        贴 /blob/main/Qwen3.8-27B-UD-Q4_K_M.gguf 时收窄后只剩 1 个文件，靠 repo_names 兜住。
        """
        siblings = [{"rfilename": "Qwen3.8-27B-UD-Q4_K_M.gguf", "size": 16 * 1024**3}]
        mf = build_model_files(
            siblings,
            "image-text-to-text",
            source_subpath="Qwen3.8-27B-UD-Q4_K_M.gguf",
            repo_names=[
                "Qwen3.8-27B-UD-Q4_K_M.gguf",
                "Qwen3.8-27B-UD-Q8_K_XL.gguf",
                "mmproj-BF16.gguf",
                "mmproj-F16.gguf",
                "imatrix_unsloth.gguf",
            ],
        )
        assert mf.mmproj_file == "mmproj-F16.gguf"
        assert mf.is_vision is True
        assert mf.suggested_type == "multimodal"

    def test_mmproj_implies_multimodal_when_no_pipeline_tag(self):
        """上游无 pipeline_tag（镜像 API 常见）但仓库带 mmproj → 直接建议 multimodal（免手选）。"""
        siblings = [{"rfilename": "model-Q4_K_M.gguf"}]
        mf = build_model_files(
            siblings,
            None,
            repo_names=["model-Q4_K_M.gguf", "mmproj-F16.gguf"],
        )
        assert mf.suggested_type == "multimodal"
        assert mf.mmproj_file == "mmproj-F16.gguf"

    def test_revision_pins_sha_when_not_explicit(self):
        # 未显式传 revision → 固定到探测到的 commit sha（防 "main" 漂移）；显式传则用显式值
        siblings = [{"rfilename": "config.json"}, {"rfilename": "model.safetensors"}]
        pinned = build_model_files(siblings, "text-generation", revision=None, sha="abc123def456")
        assert pinned.revision == "abc123def456"
        explicit = build_model_files(siblings, "text-generation", revision="v1.0", sha="abc123def456")
        assert explicit.revision == "v1.0"

    def test_mixtral_moe_no_tag(self):
        # Mixtral：pipeline_tag 真实为 None → suggested_type=None（用户选）；MoE 客观检出
        siblings = [{"rfilename": "config.json", "size": 1024}, {"rfilename": "model.safetensors", "size": 0}]
        config = {
            "architectures": ["MixtralForCausalLM"],
            "model_type": "mixtral",
            "num_experts_per_tok": 2,
            "num_local_experts": 8,
        }
        mf = build_model_files(siblings, None, config=config)
        assert mf.is_moe is True
        assert mf.architecture == "MixtralForCausalLM"
        assert mf.suggested_type is None

    def test_reranker_via_tag(self):
        # bge-reranker：text-ranking tag → rerank（不再需要架构/名字 hack）
        siblings = [{"rfilename": "config.json"}, {"rfilename": "model.safetensors"}]
        mf = build_model_files(siblings, "text-ranking")
        assert mf.suggested_type == "rerank"

    def test_flux_diffusers(self):
        # FLUX：无顶层 config.json，diffusers 结构 + text-to-image → image；pipeline_class 取自 model_index
        siblings = [
            {"rfilename": "model_index.json"},
            {"rfilename": "flux1-dev.safetensors", "size": 23 * 1024**3},
            {"rfilename": "transformer/config.json"},
        ]
        mi = {"_class_name": "FluxPipeline"}
        mf = build_model_files(siblings, "text-to-image", model_index=mi)
        assert mf.layout == "diffusers"
        assert mf.source_kind == "diffusers_folder"
        assert mf.pipeline_class == "FluxPipeline"
        assert mf.suggested_type == "image"
        assert mf.has_config is True  # model_index 存在
        assert mf.size_gb is not None and mf.size_gb > 20

    def test_gguf_single_file(self):
        siblings = [{"rfilename": "qwen2.5-7b-instruct-q4_k_m.gguf", "size": 5 * 1024**3}]
        mf = build_model_files(siblings, "text-generation")
        assert mf.structure == "single_file"
        assert mf.source_kind == "single_gguf"
        assert mf.layout == "gguf"
        assert mf.format == "gguf"
        assert mf.quantization == "q4_k_m"
        assert mf.suggested_type == "llm"

    def test_inflect_tts_via_tag(self):
        # Inflect-Nano-v2：自定义 config（无 architectures），但 HF tag=text-to-speech → tts（不靠 config 考古）
        siblings = [{"rfilename": "config.json", "size": 1024}, {"rfilename": "model.pth", "size": 0}]
        config = {"format": "inflect_v2_inference_config_v1", "model": {"upsample_rates": [8, 8, 2, 2]}}
        mf = build_model_files(siblings, "text-to-speech", config=config)
        assert mf.layout == "custom_config"
        assert mf.architecture == "inflect_v2_inference_config_v1"
        assert mf.suggested_type == "tts"

    def test_failure_keeps_partial(self):
        # config 取不到 → 仍返回文件结构 + message 提示，不抛异常
        siblings = [{"rfilename": "model.safetensors", "size": 1024}]
        mf = build_model_files(siblings, None, config_error="config.json: HTTPStatusError")
        assert mf.success is True
        assert mf.message and "config" in mf.message
        assert mf.layout == "unknown"

    def test_gguf_moe_via_remote_metadata_param(self):
        # 远程 GGUF：无 config，但探测器已 Range 探出 gguf_is_moe=True → is_moe=True
        siblings = [{"rfilename": "Phi-mini-MoE-instruct-Q8_0.gguf", "size": 5 * 1024**3}]
        mf = build_model_files(siblings, "text-generation", gguf_is_moe=True)
        assert mf.is_moe is True
        assert mf.format == "gguf"
        assert mf.success is True

    def test_gguf_moe_none_when_not_probed(self):
        # 远程 GGUF 未探（gguf_is_moe=None，如探测失败降级）→ is_moe=False（兜底）
        siblings = [{"rfilename": "model-Q4_K_M.gguf", "size": 5 * 1024**3}]
        mf = build_model_files(siblings, "text-generation")
        assert mf.is_moe is False

    def test_gguf_header_provides_moe_and_shape(self):
        # 远程 GGUF 头元数据齐备 → MoE 与架构形状一并落 ModelFiles
        from llmpod.apps.manager.model_source import gguf_meta

        siblings = [{"rfilename": "model-Q4_K_M.gguf", "size": 5 * 1024**3}]
        header = gguf_meta.GGUFMetadata(expert_count=8, num_layers=32, num_kv_heads=8, head_dim=128)
        mf = build_model_files(siblings, "text-generation", gguf_header=header)
        assert mf.is_moe is True
        assert (mf.num_layers, mf.num_kv_heads, mf.head_dim) == (32, 8, 128)

    def test_config_provides_shape(self):
        # transformers config → 三字段提取（无 GGUF 头时）
        siblings = [
            {"rfilename": "config.json", "size": 1024},
            {"rfilename": "model.safetensors", "size": 2 * 1024**3},
        ]
        config = {"num_hidden_layers": 28, "num_key_value_heads": 4, "head_dim": 128}
        mf = build_model_files(siblings, "text-generation", config=config)
        assert (mf.num_layers, mf.num_kv_heads, mf.head_dim) == (28, 4, 128)

    def test_vision_via_mmproj_in_siblings(self):
        # 仓库含 mmproj 视觉投影器 → is_vision=True（多模态信号）
        siblings = [
            {"rfilename": "model-Q4_K_M.gguf", "size": 5 * 1024**3},
            {"rfilename": "mmproj-F16.gguf", "size": 1 * 1024**3},
        ]
        mf = build_model_files(siblings, "text-generation")
        assert mf.is_vision is True

    def test_no_mmproj_is_not_vision(self):
        # 无 mmproj → is_vision=False
        siblings = [{"rfilename": "model.safetensors", "size": 1024}, {"rfilename": "config.json"}]
        config = {"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"}
        mf = build_model_files(siblings, "text-generation", config=config)
        assert mf.is_vision is False

    def test_gguf_multi_variant_returns_none_size(self):
        # 多变体 GGUF repo（不同顶层目录各含 .gguf，无 config）→ success=False（严重失败，
        # 前端禁添加）+ size=None + message 列变体提示带路径重试
        siblings = [
            {"rfilename": "Q4_K_M/model-00001-of-00002.gguf", "size": 50 * 1024**3},
            {"rfilename": "Q4_K_M/model-00002-of-00002.gguf", "size": 50 * 1024**3},
            {"rfilename": "Q8_0/model-00001-of-00002.gguf", "size": 80 * 1024**3},
            {"rfilename": "Q8_0/model-00002-of-00002.gguf", "size": 80 * 1024**3},
        ]
        mf = build_model_files(siblings, None)
        assert mf.success is False  # size 完全拿不到 → 严重失败，前端禁添加
        assert mf.size_gb is None  # 不胡乱求和
        assert mf.format == "gguf"
        assert mf.quantization is None
        assert mf.message and "Q4_K_M" in mf.message and "Q8_0" in mf.message

    def test_gguf_multi_variant_keeps_probed_moe(self):
        # 多变体 GGUF（success=False 禁添加）但探测器已探了默认变体的头判出 MoE →
        # is_moe 仍如实返回（不因多变体早返回丢弃），供前端预览
        siblings = [
            {"rfilename": "model-Q4_K_M.gguf", "size": 15 * 1024**3},
            {"rfilename": "model-Q8_0.gguf", "size": 28 * 1024**3},
        ]
        mf = build_model_files(siblings, None, gguf_is_moe=True)
        assert mf.success is False  # 多变体仍禁添加
        assert mf.is_moe is True  # ← 修复点：探到的 MoE 不丢弃
        assert mf.is_vision is False  # 无 mmproj

    def test_gguf_multi_variant_header_moe_takes_priority(self):
        """唯一调用方 inspect() 只传 gguf_header、从不传 gguf_is_moe——多变体早返回
        分支必须优先读头部，否则真实探测结果被恒丢弃（is_moe 恒 False）。"""
        from llmpod.apps.manager.model_source.gguf_meta import GGUFMetadata

        siblings = [
            {"rfilename": "model-Q4_K_M.gguf", "size": 15 * 1024**3},
            {"rfilename": "model-Q8_0.gguf", "size": 28 * 1024**3},
        ]
        header = GGUFMetadata(expert_count=8)  # 真实类型：is_moe 由 expert_count>0 派生
        mf = build_model_files(siblings, None, gguf_header=header)
        assert mf.success is False
        assert mf.is_moe is True  # 头部判定不被 gguf_is_moe=None 兜底吞掉

    def test_gguf_single_variant_multi_shard_sums(self):
        # 单变体多分片（已 subpath scope 后）→ 正常求和
        siblings = [
            {"rfilename": "Q4_K_M/model-00001-of-00002.gguf", "size": 50 * 1024**3},
            {"rfilename": "Q4_K_M/model-00002-of-00002.gguf", "size": 50 * 1024**3},
        ]
        mf = build_model_files(siblings, None)
        assert mf.size_gb == 100.0
        assert mf.source_kind == "gguf_shards"
        assert mf.format == "gguf"
        assert mf.success is True

    def test_gguf_multi_variant_root_level_returns_none(self):
        # 多变体并列在根级（无分片、无目录），含 mmproj → success=False + 列变体
        # 对应 unsloth/Qwen3.6-27B-MTP-GGUF 这种布局
        siblings = [
            {"rfilename": "Qwen3.6-27B-Q4_K_M.gguf", "size": 15 * 1024**3},
            {"rfilename": "Qwen3.6-27B-Q8_0.gguf", "size": 28 * 1024**3},
            {"rfilename": "mmproj-F16.gguf", "size": 1 * 1024**3},
        ]
        mf = build_model_files(siblings, None)
        assert mf.success is False  # size=None + 无法判定 → 严重失败
        assert mf.size_gb is None  # 不胡乱求和
        assert mf.format == "gguf"
        assert mf.message and "Q4_K_M" in mf.message and "Q8_0" in mf.message

    def test_source_subpath_propagated(self):
        """C1：source_subpath 透传到 ModelFiles（落 Model.source_subpath，供 node 收窄+verify）。"""
        siblings = [
            {"rfilename": "Q4_K_M/model-00001-of-00002.gguf", "size": 50 * 1024**3},
            {"rfilename": "Q4_K_M/model-00002-of-00002.gguf", "size": 50 * 1024**3},
        ]
        # 不带 subpath → None（缺省）
        assert build_model_files(siblings, None).source_subpath is None
        # 带 subpath → 透传
        mf = build_model_files(siblings, None, source_subpath="Q4_K_M")
        assert mf.source_subpath == "Q4_K_M"
        assert mf.source_kind == "gguf_shards"


class TestFirstGgufShard:
    """first_gguf_shard：多变体/多分片时定位「探哪个文件的头」——须避开 mmproj 投影器。"""

    def test_single_file(self):
        assert inference.first_gguf_shard(["model.gguf"]) == "model.gguf"

    def test_multi_shard_picks_first(self):
        shards = ["model-00002-of-00003.gguf", "model-00001-of-00003.gguf", "model-00003-of-00003.gguf"]
        assert inference.first_gguf_shard(shards) == "model-00001-of-00003.gguf"

    def test_multi_variant_excludes_mmproj(self):
        # 多变体 + mmproj 并列 → 必须选主模型变体，不选投影器（投影器头里没 expert_count）
        names = ["mmproj-F16.gguf", "model-Q4_K_M.gguf", "model-Q8_0.gguf"]
        chosen = inference.first_gguf_shard(names)
        assert chosen is not None
        assert "mmproj" not in chosen.lower()  # 关键：不探投影器

    def test_only_mmproj_returns_none(self):
        # 全是 mmproj（纯投影器目录，无主模型）→ 无可探 → None
        assert inference.first_gguf_shard(["mmproj-F16.gguf"]) is None


# ============ build_local_model_files：GGUF MoE 接入 ============


class TestBuildLocalGGUFMoe:
    """local GGUF 无 config 时从文件头 metadata 判 MoE（stub 解析器，隔离 IO）。"""

    def test_gguf_moe_via_metadata(self, monkeypatch):
        from llmpod.apps.manager.model_source import gguf_meta
        from llmpod.apps.manager.model_source.local import ModelProbe, build_local_model_files

        def _fake_parse(path):
            return gguf_meta.GGUFMetadata(expert_count=8, architecture="llama")

        monkeypatch.setattr(gguf_meta, "parse_gguf_metadata", _fake_parse)
        probe = ModelProbe(exists=True, weight_files=["model.gguf"], total_bytes=5 * 1024**3)

        mf = build_local_model_files(probe, "/data/x")

        assert mf.is_moe is True  # 从 metadata 判出 MoE
        assert mf.format == "gguf"
        assert mf.success is True  # size+format 齐全 → 正常

    def test_gguf_non_moe_via_metadata(self, monkeypatch):
        from llmpod.apps.manager.model_source import gguf_meta
        from llmpod.apps.manager.model_source.local import ModelProbe, build_local_model_files

        def _fake_parse(path):
            return gguf_meta.GGUFMetadata(expert_count=0, architecture="llama")

        monkeypatch.setattr(gguf_meta, "parse_gguf_metadata", _fake_parse)
        probe = ModelProbe(exists=True, weight_files=["model.gguf"], total_bytes=5 * 1024**3)

        mf = build_local_model_files(probe, "/data/x")

        assert mf.is_moe is False  # 0 专家 → 非 MoE

    def test_gguf_shape_via_metadata(self, monkeypatch):
        # GGUF 头同时提供架构形状 → 落 ModelFiles 三字段（KV 真公式输入）
        from llmpod.apps.manager.model_source import gguf_meta
        from llmpod.apps.manager.model_source.local import ModelProbe, build_local_model_files

        def _fake_parse(path):
            return gguf_meta.GGUFMetadata(num_layers=32, num_kv_heads=8, head_dim=128)

        monkeypatch.setattr(gguf_meta, "parse_gguf_metadata", _fake_parse)
        probe = ModelProbe(exists=True, weight_files=["model.gguf"], total_bytes=5 * 1024**3)

        mf = build_local_model_files(probe, "/data/x")

        assert (mf.num_layers, mf.num_kv_heads, mf.head_dim) == (32, 8, 128)

    def test_gguf_metadata_unavailable_degrades_false(self, monkeypatch):
        # 解析器返回 None（非 GGUF / 截断 / 读不到）→ is_moe 降级 False，不抛
        from llmpod.apps.manager.model_source import gguf_meta
        from llmpod.apps.manager.model_source.local import ModelProbe, build_local_model_files

        monkeypatch.setattr(gguf_meta, "parse_gguf_metadata", lambda path: None)
        probe = ModelProbe(exists=True, weight_files=["model.gguf"], total_bytes=5 * 1024**3)

        mf = build_local_model_files(probe, "/data/x")

        assert mf.is_moe is False  # 降级
        assert mf.success is True  # size+format 齐全，只是 MoE 探不到

    def test_non_gguf_skips_metadata_probe(self, monkeypatch):
        # 非 GGUF（如 safetensors）不触 metadata 解析 → is_moe=False（config 无）
        from llmpod.apps.manager.model_source import gguf_meta
        from llmpod.apps.manager.model_source.local import ModelProbe, build_local_model_files

        called = []
        monkeypatch.setattr(gguf_meta, "parse_gguf_metadata", lambda path: called.append(path) or None)
        probe = ModelProbe(exists=True, weight_files=["model.safetensors"], total_bytes=1024**3)

        mf = build_local_model_files(probe, "/data/x")

        assert mf.is_moe is False
        assert called == []  # 非 GGUF 不调解析器

    def test_size_and_format_both_missing_is_failure(self, monkeypatch):
        # size 缺 + format 缺（无权重文件）→ success=False（严重失败，前端禁添加）
        from llmpod.apps.manager.model_source.local import ModelProbe, build_local_model_files

        probe = ModelProbe(exists=True, config={"x": 1}, weight_files=[], total_bytes=0)
        mf = build_local_model_files(probe, "/data/x")

        assert mf.success is False  # size=None + format=None → 全失败


# ============ parse_hf_ref ============


class TestParseHfRef:
    def test_repo_id(self):
        # repo id：endpoint=None/subpath=None（用 settings 配置）
        assert parse_hf_ref("Qwen/Qwen2.5-0.5B") == ("Qwen/Qwen2.5-0.5B", None, None)

    def test_url_hf_co(self):
        # 完整 URL：endpoint=该 URL 的 scheme://host，优先于 settings
        assert parse_hf_ref("https://huggingface.co/Qwen/Qwen2.5-0.5B") == (
            "Qwen/Qwen2.5-0.5B",
            "https://huggingface.co",
            None,
        )

    def test_url_hf_mirror(self):
        # 贴镜像 URL：endpoint=hf-mirror.com，直接用不必改全局配置
        assert parse_hf_ref("https://hf-mirror.com/moonshotai/Kimi-K3") == (
            "moonshotai/Kimi-K3",
            "https://hf-mirror.com",
            None,
        )

    def test_url_with_tree_does_not_extract_rev(self):
        # URL 含 /tree/v1.0：endpoint 提取，分支不自动识别；subpath 无后续→None
        repo, endpoint, subpath = parse_hf_ref("https://huggingface.co/Qwen/Qwen2.5-0.5B/tree/v1.0")
        assert repo == "Qwen/Qwen2.5-0.5B" and endpoint == "https://huggingface.co" and subpath is None

    def test_url_with_resolve_does_not_extract_rev(self):
        # URL 含 /resolve/main/config.json：分支不识别；subpath=config.json
        repo, endpoint, subpath = parse_hf_ref("https://huggingface.co/Qwen/Qwen2.5-0.5B/resolve/main/config.json")
        assert repo == "Qwen/Qwen2.5-0.5B" and endpoint == "https://huggingface.co"
        assert subpath == "config.json"

    def test_url_subpath_variant_dir(self):
        # GGUF 多变体：贴变体目录路径 → subpath=变体目录
        repo, endpoint, subpath = parse_hf_ref("https://hf-mirror.com/unsloth/X-GGUF/tree/main/Q4_K_M")
        assert repo == "unsloth/X-GGUF" and endpoint == "https://hf-mirror.com"
        assert subpath == "Q4_K_M"

    def test_url_subpath_variant_file(self):
        # 贴到具体分片文件 → subpath 含目录+文件名
        repo, _endpoint, subpath = parse_hf_ref(
            "https://hf-mirror.com/unsloth/X-GGUF/resolve/main/Q4_K_M/model-00001-of-00003.gguf"
        )
        assert repo == "unsloth/X-GGUF"
        assert subpath == "Q4_K_M/model-00001-of-00003.gguf"


# ============ get_inspector 注册表 ============


class TestRegistry:
    def test_supported_sources(self):
        assert set(supported_sources()) >= {"huggingface", "oss", "local"}

    def test_get_huggingface(self):
        assert isinstance(get_inspector("huggingface"), HFHubInspector)

    def test_get_oss(self):
        assert isinstance(get_inspector("oss"), OSSInspector)

    def test_get_local(self):
        assert isinstance(get_inspector("local"), LocalInspector)

    def test_unknown_source_raises(self):
        with pytest.raises(LLMPodValidationError):
            get_inspector("ftp")

    def test_known_but_unregistered_source_raises(self):
        # ModelSource 枚举声明了 HF 别名但 _REGISTRY 未注册 → 422，不是 500
        with pytest.raises(LLMPodValidationError):
            get_inspector("hf")

    def test_model_files_is_response_schema(self):
        # inspect 响应体 schema 是 ModelFiles（含 suggested_type）
        assert "suggested_type" in ModelFiles.model_fields
        assert "layout" in ModelFiles.model_fields
        assert "num_layers" in ModelFiles.model_fields


# ============ 架构形状提取（KV 真公式 + 逐层 offload 输入） ============


class TestInferArchShape:
    def test_top_level_keys(self):
        cfg = {
            "num_hidden_layers": 28,
            "num_attention_heads": 28,
            "num_key_value_heads": 4,
            "head_dim": 128,
        }
        assert inference.infer_num_layers(cfg) == 28
        assert inference.infer_num_attention_heads(cfg) == 28
        assert inference.infer_num_kv_heads(cfg) == 4
        assert inference.infer_head_dim(cfg) == 128

    def test_mha_falls_back_to_attention_heads(self):
        # 不写 num_key_value_heads 的 MHA 模型：KV head 数 = attention heads
        cfg = {"num_hidden_layers": 12, "num_attention_heads": 12, "hidden_size": 768}
        assert inference.infer_num_kv_heads(cfg) == 12
        assert inference.infer_head_dim(cfg) == 64  # 768 ÷ 12

    def test_nested_llm_subconfig(self):
        # 多模态：语言模型参数在 text_config 里，顶层只有视觉配置
        cfg = {
            "vision_config": {"hidden_size": 1152},
            "text_config": {
                "num_hidden_layers": 32,
                "num_attention_heads": 32,
                "num_key_value_heads": 8,
                "head_dim": 128,
            },
        }
        assert inference.infer_num_layers(cfg) == 32
        assert inference.infer_num_kv_heads(cfg) == 8
        assert inference.infer_head_dim(cfg) == 128

    def test_alternate_key_names(self):
        cfg = {"n_layer": 24, "n_head": 16, "n_embd": 1024}
        assert inference.infer_num_layers(cfg) == 24
        assert inference.infer_num_attention_heads(cfg) == 16
        assert inference.infer_num_kv_heads(cfg) == 16
        assert inference.infer_head_dim(cfg) == 64

    def test_attention_heads_does_not_fall_back_to_kv_heads(self):
        # GQA 模型只写了 num_key_value_heads：attention head 数未知，不能拿 KV head 数顶替
        # （顶上会让 TP 整除校验比引擎宽，vLLM 在 config 期照样炸）
        cfg = {"num_hidden_layers": 28, "num_key_value_heads": 4}
        assert inference.infer_num_attention_heads(cfg) is None
        assert inference.infer_num_kv_heads(cfg) == 4

    def test_missing_returns_none(self):
        assert inference.infer_num_layers({}) is None
        assert inference.infer_num_attention_heads({}) is None
        assert inference.infer_num_kv_heads({}) is None
        assert inference.infer_head_dim({}) is None
        assert inference.infer_max_context({}) is None
        # 非整数 / 0 / bool 一律不采纳
        assert inference.infer_num_layers({"num_hidden_layers": 0}) is None
        assert inference.infer_num_attention_heads({"num_attention_heads": True}) is None
        assert inference.infer_num_kv_heads({"num_key_value_heads": True}) is None


class TestNeedsTrustRemoteCode:
    def test_auto_map_means_yes(self):
        assert inference.needs_trust_remote_code({"auto_map": {"AutoModel": "modeling_x.X"}}) is True

    def test_plain_config_means_no(self):
        assert inference.needs_trust_remote_code({"num_hidden_layers": 32}) is False

    def test_empty_auto_map_is_false(self):
        # 空 dict 不算（HF 侧也不会据此要求信任）
        assert inference.needs_trust_remote_code({"auto_map": {}}) is False


class TestInferMaxContext:
    """上下文上限：各候选键取最小（与 vLLM 推导同口径的下界）。"""

    def test_takes_smallest_across_keys(self):
        # 同一个 config 里同时写了两个键时取更小的那个（vLLM 也是取最小）
        cfg = {"max_position_embeddings": 4096, "model_max_length": 32768}
        assert inference.infer_max_context(cfg) == 4096

    def test_alternate_key_names(self):
        assert inference.infer_max_context({"n_positions": 2048}) == 2048
        assert inference.infer_max_context({"seq_length": 1024}) == 1024

    def test_nested_llm_subconfig(self):
        cfg = {"vision_config": {"max_position_embeddings": 4096}, "text_config": {"max_position_embeddings": 32768}}
        assert inference.infer_max_context(cfg) == 32768

    def test_ignores_junk_values(self):
        assert inference.infer_max_context({"max_position_embeddings": 0}) is None
        assert inference.infer_max_context({"max_position_embeddings": True}) is None
        assert inference.infer_max_context({"max_position_embeddings": "4096"}) is None
