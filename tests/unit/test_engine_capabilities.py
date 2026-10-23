"""引擎能力声明（适配器为唯一定义处，通用层不写引擎名/格式字面量）。

覆盖 ``supports_cpu_lane``（CPU / 内存部署准入）与 ``offload_gpu_archs``（offload 评估
计入显存的卡架构）——路由准入、部署形态求解、预检分支都消费这两个能力。
"""

from __future__ import annotations

import pytest

from llmpod.apps.manager.deployment.engine.llamacpp import LlamaCppAdapter
from llmpod.apps.manager.deployment.engine.sglang import SGLangAdapter
from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.engine.vllm import VLLMAdapter


def test_cpu_lane_capability_by_engine():
    """llama.cpp 声明 cpu 软件栈 → 支持 CPU 部署；vLLM/SGLang 不支持。"""
    assert LlamaCppAdapter.supports_cpu_lane() is True
    assert VLLMAdapter.supports_cpu_lane() is False
    assert SGLangAdapter.supports_cpu_lane() is False


def test_offload_gpu_archs_excludes_cpu_pseudo_arch():
    """offload 评估只累加引擎真能用的卡架构（cpu 是部署标记，不是卡）。"""
    assert LlamaCppAdapter.offload_gpu_archs() == ["nvidia"]
    assert "cpu" not in VLLMAdapter.offload_gpu_archs()
    assert "cpu" not in SGLangAdapter.offload_gpu_archs()


def test_multinode_capability_by_engine():
    """llama.cpp 单机引擎（无 rendezvous/集合通信）→ 多机恒拒；vLLM/SGLang 支持。
    声明管前端禁用控件（inspect/engines 端点回显），validate_cross_node 兜底硬拒。"""
    assert LlamaCppAdapter.supports_multinode is False
    assert VLLMAdapter.supports_multinode is True
    assert SGLangAdapter.supports_multinode is True


def test_base_hook_rejects_single_node_engine():
    """未声明多机的引擎被 validate_topology 的 choke point 拒（声明即全部——引擎
    不需要再覆写钩子自带拒绝路径）。llama.cpp 走同一判定。"""
    from llmpod.apps.manager.deployment.engine.base import EngineAdapter
    from llmpod.apps.manager.deployment.schemas import Topology as Topology_
    from llmpod.apps.manager.deployment.scheduler.topology import validate_topology
    from llmpod.core.exceptions import LLMPodValidationError

    class NoDeclare(EngineAdapter):
        def generate_start_args(self, instance, topology, engine_params, **kw):  # 抽象方法占位
            return []

    with pytest.raises(LLMPodValidationError, match="不支持多机部署"):
        validate_topology(Topology_(tp=2, nnodes=2), adapter=NoDeclare())


# ---- 架构闸门：可调度架构序（本拓扑服务得了 + 池内确有匹配镜像）----


class _Card:
    """任意带 uuid/node_id/arch/name/driver_version/status 的池对象（调度候选的最小形状）。"""

    def __init__(
        self, uuid, arch="nvidia", name="NVIDIA GeForce RTX 2080 Ti", driver_version="595.84", status="online"
    ):
        self.uuid = uuid
        self.node_id = "n1"
        self.index = 0
        self.arch = arch
        self.name = name
        self.driver_version = driver_version
        self.status = status


def test_arch_order_drops_arch_without_image():
    """池里只有没镜像的架构 → 该架构不进回落序（留着会让副本落到跑不起来的卡上）。"""
    pool = [_Card("c1", arch="cann", name="310P3", driver_version="25.2.3")]
    # 0.20.0 在 catalog 里没有 310P 条目
    assert get_adapter("vllm").schedulable_archs(gpu_pool=pool, engine_version="0.20.0") == []
    assert get_adapter("vllm").schedulable_archs(gpu_pool=pool, engine_version="0.23.0") == ["cann"]


def test_arch_order_drops_topology_incompatible_arch():
    """vllm 的 cann 不支持 pp>1 → 回落序里剔掉（nvidia 仍在）。"""
    pool = [
        _Card("g1", arch="nvidia"),
        _Card("c1", arch="cann", name="310P3", driver_version="25.2.3"),
    ]
    from llmpod.apps.manager.deployment.schemas import Topology

    assert get_adapter("vllm").schedulable_archs(Topology(tp=1, pp=1), gpu_pool=pool, engine_version="0.23.0") == [
        "nvidia",
        "cann",
    ]
    assert get_adapter("vllm").schedulable_archs(Topology(tp=1, pp=2), gpu_pool=pool, engine_version="0.23.0") == [
        "nvidia"
    ]
    # cann-only + pp>1 → 一个候选都不剩（上层据此在创建期拒，而不是回落默认架构）
    assert (
        get_adapter("vllm").schedulable_archs(
            Topology(tp=1, pp=2),
            gpu_pool=[_Card("c1", arch="cann", name="310P3", driver_version="25.2.3")],
            engine_version="0.23.0",
        )
        == []
    )


def test_pinned_image_never_falls_back_across_arch():
    """用户固定镜像时不跨架构回落：catalog 无从判断该镜像能在哪些架构上跑。"""
    pool = [_Card("g1", arch="nvidia"), _Card("c1", arch="cann", name="310P3", driver_version="25.2.3")]
    order = get_adapter("vllm").schedulable_archs(
        gpu_pool=pool, engine_version="0.23.0", explicit_image="my/cuda-vllm:v1"
    )
    assert order == ["nvidia"]


def test_pinned_image_keeps_existing_deployment_arch():
    """已有部署的扩容：固定镜像 + ``must_include``=本部署 arch → 只留那个 arch。

    「只留默认架构」是为建部署写的（catalog 判不出这个镜像能跑哪些架构）；套到扩容上会让
    固定 cann 镜像的部署拿 nvidia 去 NPU 池里找卡，永远扩不了容。
    """
    pool = [_Card("g1", arch="nvidia"), _Card("c1", arch="cann", name="310P3", driver_version="25.2.3")]
    order = get_adapter("vllm").schedulable_archs(
        gpu_pool=pool, engine_version="0.23.0", explicit_image="my/ascend:v1", must_include="cann"
    )
    assert order == ["cann"]


def test_pinned_image_ignores_must_include_without_image():
    """没固定镜像时 must_include 不参与——回落序仍由「池里确有匹配镜像的卡」决定。"""
    pool = [_Card("g1", arch="nvidia"), _Card("c1", arch="cann", name="310P3", driver_version="25.2.3")]
    order = get_adapter("vllm").schedulable_archs(gpu_pool=pool, engine_version="0.23.0", must_include="cann")
    assert order == ["nvidia", "cann"]


def test_topology_conflict_reason_is_engine_owned():
    """拓扑冲突的人读原因由适配器出（手选路径据此在创建期拒）。"""
    from llmpod.apps.manager.deployment.schemas import Topology

    ad = get_adapter("vllm")
    assert ad.topology_conflict("cann", Topology(tp=1, pp=2)) == "Ascend(cann) 暂不支持流水线并行（pp>1）"
    assert ad.topology_conflict("cann", Topology(tp=1, pp=1)) is None
    assert (
        ad.topology_conflict("cann", Topology(tp=4, pp=1, nnodes=2))
        == "Ascend(cann) 暂不支持多机部署（多机启动参数未组装）"
    )
    assert ad.topology_conflict("nvidia", Topology(tp=1, pp=2, nnodes=2)) is None
