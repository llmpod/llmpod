"""资源估算数值的 golden 比对：把公式输出冻成标准答案，防无声明漂移。

行为锁测试（test_inspect_*.py）断言的是文案与布尔，估算函数在那些测试里多被 stub——
公式改系数（KV 精度折算、headroom、并行均摊）只要文案模板没变就会无声溜过。本文件
直接调纯函数，全量数值与 ``tests/fixtures/golden/resource_estimates.json`` 比对。

**有意变更公式时**：设 ``LLMPOD_REGEN_GOLDEN=1`` 重跑本文件重录答案，评审 JSON diff
即「这次改动影响了哪些口径」的报告。只锁公式族（KV / 所需显存 / 每卡均摊 / 部署形态
求解 / L1 推荐数值），不锁 catalog 镜像名（catalog 数据有自己的测试）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager.deployment.engine.base import estimate_required_vram
from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter
from llmpod.apps.manager.deployment.recommend import build_recommendation
from llmpod.apps.manager.deployment.schemas import Topology
from llmpod.apps.manager.deployment.engine import get_adapter

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden" / "resource_estimates.json"

# 代表性架构形状（GQA：32 层 / 8 KV 头 / head_dim 128 → f16@8K@4 序列 = 4.00 GiB）
GQA_META = resource.KVArchMeta(num_layers=32, num_kv_heads=8, head_dim=128)


def _assert_matches_golden(family: str, actual: dict) -> None:
    """与 golden 比对；LLMPOD_REGEN_GOLDEN=1 时重录该族并放行。"""
    regen = os.environ.get("LLMPOD_REGEN_GOLDEN") == "1"
    data = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if family not in data and not regen:
        pytest.fail(
            f"golden 缺「{family}」族：设 LLMPOD_REGEN_GOLDEN=1 重跑一次生成"
            "（顺手写盘会污染受版本控制的 golden，改为显式重录）"
        )
    if regen or family not in data:
        data[family] = actual
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    assert data[family] == actual, (
        f"「{family}」数值漂移：旧 {data[family]} vs 新 {actual}"
        "（有意变更请设 LLMPOD_REGEN_GOLDEN=1 重录并评审 JSON diff）"
    )


def test_kv_cache_gb_golden():
    """KV 唯一计算入口：启发式 / 真公式 / 精度折算 / 并发序列 / 非自回归归零。"""
    _assert_matches_golden(
        "kv_cache_gb",
        {
            "heuristic_8k_f16": resource.kv_cache_gb(14.5, 8192),
            "heuristic_128k_f16": resource.kv_cache_gb(14.5, 131072),
            "heuristic_128k_fp8": resource.kv_cache_gb(14.5, 131072, dtype_bytes=1.0),
            "formula_gqa_8k_f16_default": resource.kv_cache_gb(14.5, 8192, arch_meta=GQA_META),
            "formula_gqa_8k_f16_n64": resource.kv_cache_gb(14.5, 8192, arch_meta=GQA_META, n_parallel=64),
            "formula_gqa_32k_fp8_n16": resource.kv_cache_gb(
                14.5, 32768, arch_meta=GQA_META, dtype_bytes=1.0, n_parallel=16
            ),
            "no_autoregressive_zero": resource.kv_cache_gb(14.5, 8192, has_kv_cache=False),
            "unknown_context_zero": resource.kv_cache_gb(14.5, None),
        },
    )


def test_estimate_required_vram_golden():
    """所需显存 = 权重 + KV：上下文未知乐观裸权重、架构齐备走真公式。"""
    _assert_matches_golden(
        "estimate_required_vram",
        {
            "tp1_heuristic_8k": estimate_required_vram(14.5, 8192),
            "ctx_unknown_bare_weight": estimate_required_vram(14.5, None),
            "formula_meta_8k": estimate_required_vram(14.5, 8192, arch_meta=GQA_META),
            "formula_meta_fp8": estimate_required_vram(14.5, 8192, arch_meta=GQA_META, dtype_bytes=1.0),
        },
    )


def test_per_card_estimate_golden():
    """每卡均摊 = (权重 + KV) / (tp×pp)：权重与 KV 都按并行度分摊。"""
    _assert_matches_golden(
        "per_card_estimate",
        {
            "tp1": VLLMAdapter.estimate_vram_gb(14.5, Topology(tp=1, max_model_len=8192), "llm"),
            "tp2": VLLMAdapter.estimate_vram_gb(14.5, Topology(tp=2, max_model_len=8192), "llm"),
            "tp2_pp2": VLLMAdapter.estimate_vram_gb(14.5, Topology(tp=2, pp=2, max_model_len=8192), "llm"),
            "tp4_128k": VLLMAdapter.estimate_vram_gb(14.5, Topology(tp=4, max_model_len=131072), "llm"),
            "embedding_no_kv": VLLMAdapter.estimate_vram_gb(14.5, Topology(tp=1, max_model_len=8192), "embedding"),
            "formula_meta_tp2": VLLMAdapter.estimate_vram_gb(
                14.5, Topology(tp=2, max_model_len=8192), "llm", arch_meta=GQA_META
            ),
        },
    )


def test_lane_solve_golden():
    """部署形态求解：逐层表两端取点 + 场景判定（显存满上 / 纯 CPU）。"""
    gpu_side = resource.estimate_memory_requirements(
        weight_gb=14.5, kv_gb=1.45, total_layers=32, vram_avail_gb=24.0, ram_avail_gb=64.0, allow_cpu=True
    )
    cpu_side = resource.estimate_memory_requirements(
        weight_gb=14.5, kv_gb=1.45, total_layers=32, vram_avail_gb=4.0, ram_avail_gb=64.0, allow_cpu=True
    )
    cpu_only_infeasible = resource.estimate_memory_requirements(
        weight_gb=14.5, kv_gb=1.45, total_layers=32, vram_avail_gb=24.0, ram_avail_gb=8.0, allow_cpu=True
    )

    def _project(est):
        """只比对结论面：场景 / 推荐档 / 两端档的需求值（逐层全表 32 行不进 golden）。"""
        return {
            "scenario": est.scenario,
            "recommended_tier": est.recommended_tier,
            "kv_gb": est.kv_gb,
            "tier0_ram_vram": [est.tiers[0].ram_gb, est.tiers[0].vram_gb],
            "tier_full_ram_vram": [est.tiers[-1].ram_gb, est.tiers[-1].vram_gb],
        }

    _assert_matches_golden(
        "lane_solve",
        {
            "big_vram_gpu_only": _project(gpu_side),
            "tiny_vram_cpu_only": _project(cpu_side),
            "both_pools_short_none": cpu_only_infeasible is None,
        },
    )


def test_build_recommendation_golden():
    """L1 推荐数值：tp=1 基线显存、上下文钳到模型自带上限（image 是 catalog 数据，不锁）。"""
    rec_small = build_recommendation(get_adapter("vllm"), 14.5, "llm", "vllm", context_size=8192)
    rec_clamped = build_recommendation(
        get_adapter("vllm"), 14.5, "llm", "vllm", context_size=8192, model_max_context=4096
    )
    rec_formula = build_recommendation(get_adapter("vllm"), 14.5, "llm", "vllm", context_size=8192, arch_meta=GQA_META)
    _assert_matches_golden(
        "build_recommendation",
        {
            "small_ctx8k": {
                "max_model_len": rec_small.topology["max_model_len"],
                "tp": rec_small.topology["tp"],
                "vram_per_gpu_gb": rec_small.vram_per_gpu_gb,
            },
            "clamped_to_model_max": {
                "max_model_len": rec_clamped.topology["max_model_len"],
                "vram_per_gpu_gb": rec_clamped.vram_per_gpu_gb,
            },
            "formula_meta": {"vram_per_gpu_gb": rec_formula.vram_per_gpu_gb},
        },
    )
