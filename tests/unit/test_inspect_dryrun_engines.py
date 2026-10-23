"""inspect 引擎维度的 dry-run 形状 + KV 并发口径 + CPU 部署形态回显（FakeSession 直调路由）。

三个引擎（vllm/sglang/llamacpp）各自的三层解析回显形状、image_tag 解析、n_parallel
的口径差异、CPU 部署开启后 node_support 的 cpu_lane 标记。vllm 的完整 args/env 锁定在
test_inspect_request_stages.py 的 full_stage 用例。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager.deployment.feasibility import NodeResource
from llmpod.apps.manager.deployment.schemas import Topology
from llmpod.apps.manager.deployment.schemas.config import KVCacheConfig
from llmpod.apps.webserver.routes.admin import deployment as route_mod
from llmpod.apps.webserver.services import deployment_inspect as inspect_svc
from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest
from llmpod.models.gpu import Gpu
from llmpod.models.model import Model
from llmpod.models.node import Node
from tests.unit._fakes import FakeSession


def _node(nid, mem=64.0):
    return Node(id=nid, hostname=nid, ip="10.0.0.1", cluster_id=1, status="online", memory_gb=mem)


def _gpu(i, uuid, node="n1", *, total=22.0):
    return Gpu(
        id=i,
        uuid=uuid,
        node_id=node,
        index=i,
        name="NVIDIA GeForce RTX 2080 Ti",
        arch="nvidia",
        driver_version="595.84",
        total_vram_gb=total,
        status="online",
        spec={},
    )


def _seed(model, *objs):
    s = FakeSession()
    s.preload(model)
    for o in objs:
        s.preload(o)
    return s


def _inspect(session, **kw):
    base = dict(model_id=1, cluster_id=1, engine="vllm", topology=Topology(tp=1, max_model_len=8192))
    base.update(kw)
    return route_mod.inspect_deployment(InspectDeploymentRequest(**base), session)


_M = Model(id=1, name="m", model_type="llm", size_gb=1.0, format="safetensors")
_M_GGUF = Model(id=1, name="m", model_type="llm", size_gb=1.0, format="gguf", num_layers=32)


class TestEngineDryRunShapes:
    """各引擎 dry-run 的镜像 / 参数 / 环境变量形状（三层合并后的终态）。"""

    async def test_sglang_resolved_shape(self):
        # 不选卡的通用 dry-run 无 GPU 型号/驱动上下文，SGLang 0.5.x 线要求已知
        # SM80+ 型号不参与，回落 cu118 线（0.4.2 词表：无 auto parser、有 metrics）。
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine="sglang")
        resolved = result["data"]["resolved"]
        assert result["data"]["deployable"] is True
        assert resolved["image"] == "lmsysorg/sglang:v0.4.2-cu118"
        assert resolved["args"] == [
            "--model-path",
            "<model-1>",
            "--served-model-name",
            "<model-1>",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--tp-size",
            "1",
            "--context-length",
            "8192",
            "--mem-fraction-static",
            "0.9",
            "--enable-metrics",
        ]
        assert resolved["env"] == {}  # 系统注入（HF_*）不回显，只透传用户自定义 env
        assert resolved["gpu_memory_utilization"] == 0.9  # sglang 的显存 flag 同口径回显

    async def test_llamacpp_resolved_shape(self):
        """llamacpp：默认全量上卡（--n-gpu-layers 999）、端口 8080、alias 替代 served-name（驱动未知→cu130 default）。"""
        s = _seed(_M_GGUF, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine="llamacpp")
        resolved = result["data"]["resolved"]
        assert resolved["image"] == "ghcr.io/ggml-org/llama.cpp:server-cuda13"
        assert resolved["args"] == [
            "--model",
            "<model-1>",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--alias",
            "<model-1>",
            "--ctx-size",
            "8192",
            "--n-gpu-layers",
            "999",
        ]
        assert resolved["env"] == {}  # HF_HUB_OFFLINE 是系统注入，不回显

    async def test_image_tag_resolution_for_dryrun(self):
        """image_tag 参与三层解析：0.27.1 在 595.84 命中 cu130 条目；推荐镜像仍是 catalog 无卡维度的默认。"""
        s = _seed(_M, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, image_tag="0.27.1")
        d = result["data"]
        assert d["resolved"]["image"] == "vllm/vllm-openai:v0.27.1"
        assert d["recommendation"]["image"] == "vllm/vllm-openai:v0.27.1"  # 推荐层不带 tag 维度


class TestKvParallelScope:
    """KV 并发（n_parallel）的口径：逐卡 required 跟随，推荐显存不跟随（现状）。"""

    async def test_n_parallel_scales_required_but_not_recommendation(self):
        """n_parallel=4 → cluster_gpus.required_vram_gb 抬升（KV ×4），而
        recommendation.vram_per_gpu_gb 不变（build_recommendation 未传 n_parallel）。
        两口径现状即如此，锁定防迁移时悄悄对齐（对齐与否属行为变更，须显式决策）。"""
        m = Model(
            id=1,
            name="m",
            model_type="llm",
            size_gb=10.0,
            format="safetensors",
            num_layers=32,
            num_kv_heads=8,
            head_dim=128,
        )
        s = _seed(m, _node("n1"), _gpu(1, "g1"))
        base = await _inspect(s, kv_cache=KVCacheConfig(n_parallel=1))
        scaled = await _inspect(s, kv_cache=KVCacheConfig(n_parallel=4))
        base_req = base["data"]["cluster_gpus"][0]["required_vram_gb"]
        scaled_req = scaled["data"]["cluster_gpus"][0]["required_vram_gb"]
        assert base_req == 11.0
        assert scaled_req == 14.0  # KV 项随并发放大
        assert (
            base["data"]["recommendation"]["vram_per_gpu_gb"]
            == scaled["data"]["recommendation"]["vram_per_gpu_gb"]
            == 14.0
        )


class TestEmptyCluster:
    """集群无在线节点：各视图为空、不误报（「无数据」≠「不可部署」）。"""

    async def test_cluster_without_nodes_returns_empty_views(self):
        s = _seed(_M)
        result = await _inspect(s)
        d = result["data"]
        assert d["deployable"] is True
        assert d["issues"] == []
        assert d["cluster_gpus"] == []
        assert d["node_support"] == []
        assert d["recommended_placement"] is None


def _fact(node_id: str, avail_ram_gb: float) -> NodeResource:
    return NodeResource(
        cluster_id=1,
        cluster_name="c1",
        node_id=node_id,
        hostname=node_id,
        nvidia_gpus=0,
        avail_vram_gb=0.0,
        max_card_vram_gb=0.0,
        avail_ram_gb=avail_ram_gb,
        memory_gb=avail_ram_gb,
        realtime_vram=False,
        realtime_ram=False,
    )


@pytest.fixture
def lane_patched(monkeypatch):
    """CPU 部署形态离线桩：节点资源两节点各 60GB 可用、feasibility 全绿、预览为空。"""

    async def _facts(_db, _cluster_id=None, **_kw):
        return [_fact("n1", 60.0), _fact("n2", 60.0)]

    async def _feas(_db=None, **kw):
        return SimpleNamespace(deployable=True, message="ok", deployable_clusters=[1])

    async def _preview(*_a, **_kw):
        return None

    monkeypatch.setattr(inspect_svc, "collect_node_resources", _facts)
    monkeypatch.setattr(inspect_svc, "assess_feasibility", _feas)
    monkeypatch.setattr(inspect_svc, "build_preview_topology", _preview)
    return monkeypatch


class TestCpuLaneNodeSupport:
    """allow_cpu 开启后 node_support 的补充语义（卡视角之外的内存视角）。"""

    async def test_cardless_node_selectable_via_cpu(self, lane_patched):
        """无卡节点内存够 → selectable=True 且 cpu_lane=True（前端按此标注「内存部署」）。"""
        s = _seed(_M_GGUF, _node("n1"), _node("n2"), _gpu(1, "g1"))
        result = await _inspect(s, engine="llamacpp", allow_cpu=True)
        by_id = {ns["node_id"]: ns for ns in result["data"]["node_support"]}
        assert by_id["n1"]["selectable"] is True
        assert by_id["n1"]["cpu_lane"] is False  # 有卡节点走 GPU 视角
        assert by_id["n2"]["selectable"] is True
        assert by_id["n2"]["cpu_lane"] is True
        assert by_id["n2"]["reason"] is None

    async def test_available_memory_echoed_from_facts(self, lane_patched):
        """available_memory_gb 是账本口径（总内存×0.9=57.6，扣预留），不是节点资源的
        实时口径（60.0=总-已用），也不是总内存（64.0）——与准入判定（assess_cpu_capacity）
        同源，展示与判定才不会一边绿一边红。"""
        s = _seed(_M_GGUF, _node("n1"), _node("n2"), _gpu(1, "g1"))
        result = await _inspect(s, engine="llamacpp", allow_cpu=True)
        by_id = {ns["node_id"]: ns for ns in result["data"]["node_support"]}
        assert by_id["n1"]["memory_gb"] == 64.0
        # 事实桩的 memory_gb=60 → 账本口径 60×0.9=54.0（实时口径是 60.0、DB 总内存 64.0）
        assert by_id["n1"]["available_memory_gb"] == 54.0

    async def test_gpu_only_scenario_solved_with_cards(self, lane_patched):
        """有卡集群 + allow_cpu（未显式 cpu_only）→ 解出全量上卡档（gpu_only），
        memory 逐层表照常返回（33 档 = num_layers+1 边界）。"""
        s = _seed(_M_GGUF, _node("n1"), _gpu(1, "g1"))
        result = await _inspect(s, engine="llamacpp", allow_cpu=True)
        memory = result["data"]["memory"]
        assert memory is not None
        assert memory["scenario"] == resource.SCENARIO_GPU_ONLY
        assert len(memory["tiers"]) == 33  # 32 层 + 纯 CPU 档
        assert (
            result["data"]["resolved"]["args"][result["data"]["resolved"]["args"].index("--n-gpu-layers") + 1] == "999"
        )
