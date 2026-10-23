"""KV cache 精度：选项声明、dtype 字节反解、估算折算、llama.cpp flash-attn 兜底。

选项表由各 adapter 声明（引擎原生档位），前端 Step2 高级参数下拉与后端显存估算
共用同一张表：下拉值落 ``engine_params.extra_args``，估算按主 flag 反解 ``dtype_bytes``。
"""

from __future__ import annotations

import asyncio

import pytest

from llmpod.apps.manager.deployment import resource as R
from llmpod.apps.manager.deployment.engine.base import estimate_required_vram
from llmpod.apps.manager.deployment.engine.llamacpp import LlamaCppAdapter
from llmpod.apps.manager.deployment.engine.sglang import SGLangAdapter
from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter
from llmpod.apps.manager.deployment.schemas import InstanceSpec, Topology

ALL_ADAPTERS = [VLLMAdapter, SGLangAdapter, LlamaCppAdapter]
ADAPTER_IDS = [adapter.__name__ for adapter in ALL_ADAPTERS]


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


# ============ 选项声明（后端是选项列表的唯一定义处） ============


class TestOptionDeclaration:
    @pytest.mark.parametrize("adapter", ALL_ADAPTERS, ids=ADAPTER_IDS)
    def test_first_option_is_unquantized_default(self, adapter):
        """首项 value 为空串 = 默认不量化（不写 flag），字节数为 f16 满额。"""
        options = adapter.kv_cache_dtype_options
        assert options, f"{adapter.__name__} 未声明 KV 精度选项"
        assert options[0].value == ""
        assert options[0].dtype_bytes == R.DEFAULT_KV_DTYPE_BYTES
        assert options[0].kv_ratio == 1.0
        assert options[0].args == []

    @pytest.mark.parametrize("adapter", ALL_ADAPTERS, ids=ADAPTER_IDS)
    def test_options_carry_native_flag(self, adapter):
        """每个非默认档的 args 必须含「主 flag + 该档取值」，否则下拉选了等于没选。"""
        flag = adapter.kv_cache_dtype_flags[0]
        assert flag, f"{adapter.__name__} 未声明主 flag"
        for option in adapter.kv_cache_dtype_options[1:]:
            assert option.value
            assert flag in option.args
            assert option.args[option.args.index(flag) + 1] == option.value
            # 字节数上界 = f16；显式档（如 SGLang bf16）与默认同档，不得声称比不量化更省
            assert 0 < option.dtype_bytes <= R.DEFAULT_KV_DTYPE_BYTES
            assert option.label and option.hint
        # 每引擎至少有一档真正压缩 KV，否则这张表对显存估算没有意义
        assert any(option.dtype_bytes < R.DEFAULT_KV_DTYPE_BYTES for option in adapter.kv_cache_dtype_options)

    @pytest.mark.parametrize("adapter", ALL_ADAPTERS, ids=ADAPTER_IDS)
    def test_kv_flag_not_protected(self, adapter):
        """KV 精度是用户可选项，不能被系统必控集吞掉（含全部等价写法）。"""
        for flag in adapter.kv_cache_dtype_flags:
            assert flag not in adapter.protected_flags


# ============ 反解（extra_args 是唯一定义处） ============


class TestKvCacheDtypeFrom:
    def test_absent_or_default_is_f16(self):
        assert VLLMAdapter().kv_cache_dtype_bytes_from(None) == 2.0
        assert VLLMAdapter().kv_cache_dtype_bytes_from({}) == 2.0
        assert VLLMAdapter().kv_cache_dtype_bytes_from({"extra_args": []}) == 2.0

    def test_matches_option_value(self):
        adapter = VLLMAdapter()
        assert adapter.kv_cache_dtype_bytes_from({"extra_args": ["--kv-cache-dtype", "fp8_e4m3"]}) == 1.0
        assert adapter.kv_cache_dtype_bytes_from({"extra_args": ["--kv-cache-dtype=fp8_e5m2"]}) == 1.0

    def test_bytes_per_engine_vocabulary(self):
        """同一语义、不同引擎词汇：llama.cpp q4_0 是 0.5 字节，vLLM 无此档。"""
        assert (
            LlamaCppAdapter().kv_cache_dtype_bytes_from(
                {"extra_args": ["--cache-type-k", "q4_0", "--cache-type-v", "q4_0"]}
            )
            == 0.5
        )
        assert (
            LlamaCppAdapter().kv_cache_dtype_bytes_from(
                {"extra_args": ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]}
            )
            == 1.0
        )

    def test_auxiliary_flag_removal_keeps_bytes(self):
        """用户删掉依赖 flag（--flash-attn）不该让估算口径漂移：只认主 flag。"""
        assert LlamaCppAdapter().kv_cache_dtype_bytes_from({"extra_args": ["--cache-type-k", "q8_0"]}) == 1.0

    def test_unknown_value_falls_back_conservative(self):
        """未收录取值（手写 q4_1 之类）回退 f16：宁可高估 KV，不可低估导致 OOM。"""
        assert LlamaCppAdapter().kv_cache_dtype_bytes_from({"extra_args": ["--cache-type-k", "q4_1"]}) == 2.0
        assert VLLMAdapter().kv_cache_dtype_bytes_from({"extra_args": ["--kv-cache-dtype", "bogus"]}) == 2.0

    def test_short_flag_aliases_are_recognized(self):
        """短名等价写法（-ctk）同样反解命中：漏认会退成 f16 高估、误判显存不足。"""
        adapter = LlamaCppAdapter()
        assert adapter.kv_cache_dtype_bytes_from({"extra_args": ["-ctk", "q4_0", "-ctv", "q4_0"]}) == 0.5
        assert adapter.kv_cache_dtype_bytes_from({"extra_args": ["-ctk=q8_0"]}) == 1.0

    def test_engine_without_kv_options(self):
        """未声明能力（主 flag 为空）恒 f16，不因 extra_args 里有同名 flag 而误判。"""

        class _NoKv(VLLMAdapter):
            kv_cache_dtype_flags = []

        assert _NoKv().kv_cache_dtype_bytes_from({"extra_args": ["--kv-cache-dtype", "fp8_e4m3"]}) == 2.0


# ============ 估算折算 ============


class TestEstimateWithDtype:
    def test_kv_term_scales_with_dtype_bytes(self):
        # 10GB 权重 @8192 → KV 基数 1GB；fp8（1 字节）→ 10.5，q4_0（0.5 字节）→ 10.25
        assert estimate_required_vram(10.0, 8192, dtype_bytes=1.0) == 10.5
        assert estimate_required_vram(10.0, 8192, dtype_bytes=0.5) == 10.25

    def test_formula_path_via_arch_meta(self):
        meta = R.KVArchMeta(num_layers=32, num_kv_heads=8, head_dim=128)
        # 真公式 @8192/1 字节 = 2GB
        assert estimate_required_vram(10.0, 8192, dtype_bytes=1.0, arch_meta=meta) == 12.0

    def test_ratio_ignored_without_kv(self):
        """无 KV cache（embedding/rerank）时精度档无意义，权重不随精度变。"""
        assert estimate_required_vram(10.0, 8192, has_kv_cache=False, dtype_bytes=0.5) == 10.0

    def test_per_gpu_estimate_divides_by_parallel(self):
        # tp=2：10.5 / 2 = 5.25
        assert VLLMAdapter.estimate_vram_gb(10.0, Topology(tp=2, max_model_len=8192), "llm", dtype_bytes=1.0) == 5.25


# ============ llama.cpp flash-attn 兜底 ============


class TestLlamaCppFlashAttn:
    def _assemble(self, extra: list[str]) -> list[str]:
        return LlamaCppAdapter().assemble_args(_inst(), Topology(tp=1), None, {"extra_args": extra})

    def test_injected_for_quantized_cache_type(self):
        args = self._assemble(["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"])
        assert args.count("--flash-attn") == 1

    def test_injected_for_short_flag(self):
        args = self._assemble(["-ctk", "q4_0"])
        assert "--flash-attn" in args

    def test_not_injected_for_f16(self):
        args = self._assemble(["--cache-type-k", "f16", "--cache-type-v", "f16"])
        assert "--flash-attn" not in args

    def test_not_duplicated_when_already_present(self):
        """下拉选项 args 已自带 --flash-attn，合并后不能出现两次。"""
        option = next(o for o in LlamaCppAdapter.kv_cache_dtype_options if o.value == "q8_0")
        args = self._assemble(list(option.args))
        assert args.count("--flash-attn") == 1
        assert args.count("--cache-type-k") == 1

    def test_no_extra_args_untouched(self):
        args = self._assemble([])
        assert "--flash-attn" not in args


# ============ 能力暴露（前端数据源） ============


class TestCapabilityExposure:
    def test_engines_route_exposes_options(self):
        """GET /admin/inference-images/engines 是 Step2 下拉的数据源。"""
        from llmpod.apps.webserver.routes.admin.inference_image import list_engines

        payload = asyncio.run(list_engines(model_id=None, db=None))
        by_engine = {item.engine: item for item in payload["data"]}
        for name in ("vllm", "sglang", "llamacpp"):
            assert by_engine[name].kv_cache_dtype_options
            first = by_engine[name].kv_cache_dtype_options[0]
            assert first["value"] == "" and first["dtype_bytes"] == 2.0

    def test_engines_route_exposes_multinode_flag(self):
        """supports_multinode 回显：前端据此禁用单机引擎的 nnodes 控件（llama.cpp False）。"""
        from llmpod.apps.webserver.routes.admin.inference_image import list_engines

        payload = asyncio.run(list_engines(model_id=None, db=None))
        by_engine = {item.engine: item for item in payload["data"]}
        assert by_engine["vllm"].supports_multinode is True
        assert by_engine["sglang"].supports_multinode is True
        assert by_engine["llamacpp"].supports_multinode is False

    def test_engines_route_judges_model_support(self):
        """带 model_id：返回 supports_model/unsupported_reason（前端 Step1 置灰不支持的引擎）。"""
        from llmpod.apps.webserver.routes.admin.inference_image import list_engines
        from llmpod.models.model import Model

        class _DB:
            async def get(self, cls, pk):
                return Model(id=1, name="m", model_type="llm", format="safetensors", size_gb=1.0)

        payload = asyncio.run(list_engines(model_id=1, db=_DB()))
        by_engine = {item.engine: item for item in payload["data"]}
        assert by_engine["vllm"].supports_model is True
        assert by_engine["llamacpp"].supports_model is False
        assert "格式 safetensors" in (by_engine["llamacpp"].unsupported_reason or "")

        # 不传 model_id → 不判定（前端放宽，不误灰）
        payload = asyncio.run(list_engines(model_id=None, db=None))
        assert all(item.supports_model is None for item in payload["data"])

    def test_engines_route_supports_gguf_multimodal(self):
        """GGUF 多模态（如 Qwen-VL）：llama.cpp 声明 MULTIMODAL → 不再被置灰。"""
        from llmpod.apps.webserver.routes.admin.inference_image import list_engines
        from llmpod.models.model import Model

        class _DB:
            async def get(self, cls, pk):
                return Model(id=2, name="vl", model_type="multimodal", format="gguf", size_gb=20.0)

        payload = asyncio.run(list_engines(model_id=2, db=_DB()))
        by_engine = {item.engine: item for item in payload["data"]}
        assert by_engine["llamacpp"].supports_model is True
        assert by_engine["llamacpp"].unsupported_reason is None

    def test_topology_engine_summary_exposes_options(self):
        """集群拓扑的 EngineSummary 同口径（拓扑页与向导共用）。"""
        from llmpod.apps.manager.deployment import image_catalog
        from llmpod.apps.manager.deployment.topology import _engine_summary

        image_catalog.reload()
        assert _engine_summary("vllm").kv_cache_dtype_options
        assert _engine_summary("vllm").kv_cache_dtype_options[0]["value"] == ""
