"""拓扑展开（纯函数）+ 结构校验（逐份）。

standard 模式（tensor_parallel，逐份消费各自 tp/pp/nnodes）：
- 每份 ``nnodes == 1``：该副本 = 1 个 instance = 单节点 TP/PP（节点内多 GPU 由引擎 mp 处理）；
- 每份 ``nnodes > 1``：该副本 = nnodes 个 instance（node_rank=0 为 head，其余为 headless
  worker；tp 为该份全局并行度，均分到各节点——每节点 tp×pp//nnodes 卡，由 GpuScheduler
  分配），调度约束：同 replica 的 nnodes 个 instance 落在 nnodes 个不同节点。

异构副本（``copies`` 为模板列表）逐份独立校验/展开，各份形状互不影响（总卡数 = Σ 各份）。

``validate_topology`` 是结构非法的唯一 choke point（expand 入口 + create 路由早拒共用）；
跨机整除规则经 ``adapter.validate_cross_node`` 钩子判定（引擎知识归引擎载体：默认
tp 整除 nnodes，vLLM torch-elastic 允许 tp×pp 参与跨机分摊）。容量不足不在此判
（由调度器/预检处理）。

引擎可覆盖 ``EngineAdapter.expand_topology`` 实现非对称异构展开（默认走 ``expand_standard``），
编排器直接调 ``adapter.expand_topology(topology)``——无需额外分发包装类。
"""

from __future__ import annotations

from llmpod.apps.manager.deployment import parallel
from llmpod.apps.manager.deployment.schemas import GpuSelector, InstanceSpec, Topology
from llmpod.core.exceptions import LLMPodValidationError


def _validate_unit(
    unit,
    *,
    seq: int,
    heterogeneous: bool,
    gpu_selector: GpuSelector | None,
    adapter=None,
    shape: parallel.ParallelShape | None = None,
) -> None:
    """单份结构校验；heterogeneous 时错误信息带份序号（同形保持历史消息逐字节不变）。"""
    tag = f"第 {seq + 1} 份：" if heterogeneous else ""

    def _reject(msg: str):
        raise LLMPodValidationError(f"{tag}{msg}")

    nnodes = int(unit.nnodes) if unit.nnodes is not None else 1
    tp = max(1, int(unit.tp or 1))
    pp = max(1, int(unit.pp or 1))
    if nnodes < 1:
        _reject(f"topology.nnodes 必须 ≥1（当前 {unit.nnodes}）")
    if unit.pp is not None and unit.pp < 1:
        _reject(f"topology.pp 必须 ≥1（当前 {unit.pp}）")
    if unit.ep is not None and unit.ep != 1:
        _reject(f"topology.ep 暂不支持（当前仅 ep=1；调度/展开按 tp×pp 分配卡，ep 未参与）：{unit.ep}")
    if shape is not None:
        # 权重切不开是引擎层面必然失败（且引擎未必提前报错），结构类硬拒，不受 force 影响
        reason = parallel.tp_reject_reason(shape, tp)
        if reason:
            _reject(reason)
    if nnodes > 1:
        # 单机引擎的多机硬拒放这里（choke point，覆写钩子也绕不过）：声明
        # supports_multinode=False 即拒，引擎无需再自带一条拒绝路径
        if adapter is not None and not adapter.supports_multinode:
            _reject(f"引擎不支持多机部署（nnodes={nnodes}）；请改用单机多卡或多副本")
        if adapter is not None:
            adapter.validate_cross_node(tp, pp, nnodes)
        elif tp % nnodes != 0:
            # 无 adapter 语境的保守默认（与历史行为一致）；引擎分叉语义见各 adapter 覆盖
            _reject(f"topology.tp({tp}) 必须能被 nnodes({nnodes}) 整除（每节点 tp×pp/nnodes 卡）")
        if gpu_selector is not None and gpu_selector.node_id:
            _reject("跨机部署不支持指定单个节点（跨机请用 gpu_uuids 或 node_ids）")


def validate_topology(
    topology: Topology,
    gpu_selector: GpuSelector | None = None,
    adapter=None,
    shape: parallel.ParallelShape | None = None,
    cpu_lane: bool = False,
) -> None:
    """拓扑结构校验（非法即 raise LLMPodValidationError → 路由 422 / _plan 失败）。

    支持矩阵：单/多副本（replicas≥1）、异构副本（copies 模板列表）、单份与多副本均可
    跨机（nnodes>1）；手选卡（gpu_uuids）支持单机多副本（每份卡须同节点同 arch，分组
    配对在 ``pair_manual_replicas``），多副本×跨机×手选拒绝（配对组合爆炸，走自动调度）；
    选节点（node_ids）仅同形单副本；指定单节点不跨机。容量/在线/arch/VRAM 不在此判
    （由 scheduler + feasibility 处理）。``adapter``：跨机整除钩子的载体（None=保守默认）。
    ``shape``：模型 head 形状（``parallel.ParallelShape``）；给了才校验 tp 能否切开权重，
    None/未探测 → 放行（见 ``parallel`` 模块）。
    ``cpu_lane``：显式 cpu_only 请求——``node_ids`` 语义切换为**逐副本指定**（第 i 份放
    node_ids[i]，长度须等于副本数），GPU 侧的 nnodes 配对/多副本限制不适用（CPU 部署
    恒单机单份一实例）。
    """
    units = topology.per_copy_units()
    heterogeneous = not topology.is_homogeneous
    replicas = max(1, int(topology.replicas or 1))
    if topology.replicas is not None and topology.replicas < 1:
        raise LLMPodValidationError(f"topology.replicas 必须 ≥1（当前 {topology.replicas}）")
    for seq, unit in enumerate(units):
        _validate_unit(
            unit, seq=seq, heterogeneous=heterogeneous, gpu_selector=gpu_selector, adapter=adapter, shape=shape
        )

    if gpu_selector is not None:
        has_node = gpu_selector.node_id is not None
        has_uuids = bool(gpu_selector.gpu_uuids)
        has_node_ids = bool(gpu_selector.node_ids)
        # 三种选择模式互斥
        if sum([has_node, has_uuids, has_node_ids]) > 1:
            raise LLMPodValidationError("gpu_selector 的 node_id / gpu_uuids / node_ids 互斥")
        any_cross = any(int(u.nnodes or 1) > 1 for u in units)
        if has_uuids and any_cross and replicas > 1:
            raise LLMPodValidationError("手动选卡暂不支持多副本×跨机（多机多副本请用自动调度）")
        if has_node and any_cross:
            raise LLMPodValidationError("跨机部署不支持指定单个节点（跨机请用 gpu_uuids 或 node_ids）")
        if has_node_ids and cpu_lane:
            # CPU 通道（显式 cpu_only）：node_ids = 逐副本指定，长度即份数
            if len(gpu_selector.node_ids) != replicas:
                raise LLMPodValidationError(
                    f"CPU 部署 node_ids 须逐副本给出（长度 {len(gpu_selector.node_ids)} ≠ 副本数 {replicas}）"
                )
        elif has_node_ids:
            # GPU 侧语义：候选节点集（单副本跨机配对）
            if heterogeneous:
                raise LLMPodValidationError("选节点（node_ids）暂不支持异构副本（请用自动调度或 gpu_uuids）")
            if replicas > 1:
                raise LLMPodValidationError("选节点（node_ids）暂不支持多副本")
            if len(gpu_selector.node_ids) != max(1, int(units[0].nnodes or 1)):
                raise LLMPodValidationError(
                    f"node_ids 数量须等于 nnodes（{units[0].nnodes}，当前 {len(gpu_selector.node_ids)}）"
                )


def cpu_lane_pins(gpu_selector: GpuSelector | None, offload_preference: str) -> list[str] | None:
    """显式 cpu_only 请求的逐副本指定编排（``gpu_selector.node_ids``，第 i 份放第 i 个）。

    唯一定义处：inspect 的求解/调度结果与 create 的调度共用——判定散写会漂移成
    「预检按指定、创建按 binpack」。其余请求形态（allow_cpu+auto 等）返回 None，
    node_ids 仍是 GPU 侧候选集语义，不参与 CPU 调度。
    """
    from llmpod.apps.manager.deployment.resource import SCENARIO_CPU_ONLY

    if gpu_selector and gpu_selector.node_ids and offload_preference == SCENARIO_CPU_ONLY:
        return list(gpu_selector.node_ids)
    return None


def validate_cpu_lane(topology: Topology) -> None:
    """纯 CPU 部署（``--n-gpu-layers 0``）的额外结构约束：单机单卡形状。

    CPU 部署没有「卡」的概念，tp/pp/nnodes 参与只会让引擎按多进程切模型（CPU 上无意义且
    llama.cpp 不支持 TP）。要跨节点请用多副本（每份落一个节点）。
    """
    if not topology.is_homogeneous:
        raise LLMPodValidationError("纯 CPU 部署不支持异构副本（各份形状须一致）")
    unit = topology.per_copy_units()[0]
    if max(1, int(unit.tp or 1)) > 1 or max(1, int(unit.pp or 1)) > 1 or max(1, int(unit.nnodes or 1)) > 1:
        raise LLMPodValidationError("纯 CPU 部署要求 tp=pp=1 且 nnodes=1（跨节点请改用多副本）")


def unit_parallel(topology: Topology) -> int:
    """首份的并行度 tp×pp（逐层表的显存侧按卡均摊用，与 ``estimate_vram_gb`` 同口径）。"""
    unit = topology.per_copy_units()[0]
    return max(1, int(unit.tp or 1)) * max(1, int(unit.pp or 1))


def expand_standard(topology: Topology, adapter=None) -> list[InstanceSpec]:
    """标准拓扑展开（结构校验见 :func:`validate_topology`，逐份消费各自 tp/pp/nnodes）。

    返回 InstanceSpec 列表（未分配 node/gpu，由 GpuScheduler + RankResolver 填）。
    ``adapter`` 仅透传给结构校验（跨机整除钩子）。
    """
    validate_topology(topology, adapter=adapter)

    instances: list[InstanceSpec] = []
    for i, unit in enumerate(topology.per_copy_units()):
        tp = max(1, int(unit.tp or 1))
        pp = max(1, int(unit.pp or 1))
        nnodes = int(unit.nnodes) if unit.nnodes is not None else 1
        if nnodes <= 1:
            instances.append(
                InstanceSpec(
                    role="full",
                    tp=tp,
                    pp=pp,
                    replica_idx=i,
                    nnodes=1,
                    node_rank=0,
                )
            )
            continue
        # 跨机：head（node_rank=0）+ headless workers；tp 为该份全局并行度（每节点卡数由调度器均分）
        for j in range(nnodes):
            instances.append(
                InstanceSpec(
                    role="head" if j == 0 else "worker",
                    tp=tp,
                    pp=pp,
                    replica_idx=i,
                    nnodes=nnodes,
                    node_rank=j,
                )
            )
    return instances
