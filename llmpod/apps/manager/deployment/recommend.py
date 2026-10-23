"""部署配置推荐（纯函数，零 DB / 业务依赖）。

与校验（validate_topology / assess_feasibility）和业务（deployment_service / orchestrator）完全解耦。
调用方（route handler）负责 DB 查询后将数据喂入这些纯函数。

推荐策略：默认全 adapter 默认值；用户未显式指定 tp 时由路由侧 ``derive_tp`` 按集群可选卡推导
最小可行 tp（仅此一项智能推导，其余拓扑参数不推导）。
集群推荐：遍历集群 GPU 池（调用方传入），优先单节点可行、其次跨机可行。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from llmpod.apps.manager.deployment import parallel
from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager.deployment.engine import get_adapter, registered_engines
from llmpod.apps.manager.deployment.schemas import GpuSelector, Topology
from llmpod.apps.manager.deployment.scheduler.gpu import card_reserve, reserve_fits


@dataclass
class GpuPoolEntry:
    """单个 GPU 的可用资源（调用方从 DB + gpu_buffer 装配后传入）。"""

    uuid: str
    node_id: str
    arch: str
    total_vram_gb: float
    available_vram_gb: float


@dataclass
class Recommendation:
    """推荐配置（adapter 默认值）。"""

    engine: str
    image: str
    topology: dict[str, Any] = field(default_factory=dict)
    vram_per_gpu_gb: float = 0.0
    # 引擎声明的并行能力（tp/pp/ep 白名单）；inspect 回显给前端做 pp 门控
    supported_parallel: list[str] = field(default_factory=list)


@dataclass
class ClusterPick:
    """集群推荐结果。"""

    cluster_id: int
    cross_node: bool


def recommend_engine(model_type: str | None, model_format: str | None = None) -> str | None:
    """按 model_type（+ format）匹配首个支持的引擎。

    遍历 ``registered_engines()``，返回首个 ``model_type ∈ supported_categories`` 的引擎；
    ``model_format`` 已知时还需 ``format ∈ supported_formats``——否则会把 gguf 模型错配给只支持
    safetensors 的 vllm/sglang（注册表顺序里它们排在 llamacpp 前）。model_type 为 None 或无匹配 → None。
    """
    if not model_type:
        return None
    for engine in registered_engines():
        adapter = get_adapter(engine)
        if model_type not in adapter.supported_categories:
            continue
        if model_format is not None and model_format not in adapter.supported_formats:
            continue
        return engine
    return None


def build_recommendation(
    adapter: Any,
    model_size_gb: float | None,
    model_type: str | None,
    engine_name: str,
    context_size: int | None = None,
    kv_dtype_bytes: float | None = None,
    arch_meta=None,
    n_parallel: int | None = None,
    model_max_context: int | None = None,
) -> Recommendation:
    """组装推荐配置（全 adapter 默认值，不做智能推导）。

    ``adapter`` 是 ``get_adapter(name)`` 返回的无状态实例。
    ``context_size`` 未传时按 ``KV_BASE_CONTEXT``(8192) 兜底——推荐显存含 KV，与 deploy 口径一致，
    避免「推荐集群能过、部署时显存不够」。调用方可透传 inspect 请求的 max_model_len。
    ``model_max_context``：模型自带上下文上限，已知时把推荐值钳到它以内。8192 这个兜底值对
    短上下文模型（2048/4096）是超限的，引擎会直接拒启——钳制是必需的，不是优化。
    ``kv_dtype_bytes``/``arch_meta``：请求里已选的 KV 精度档与模型架构形状，推荐显存随之折算。
    镜像：catalog 命中优先（engine+arch 粗匹配，无具体卡），否则 adapter 默认。
    """
    from llmpod.apps.manager.deployment import image_catalog

    arch = adapter.default_arch
    ctx = context_size or resource.KV_BASE_CONTEXT
    if model_max_context and model_max_context > 0:
        # 非正数当「未知」：0 会被 min 到 ctx=0（KV 估算随之归零，按只算权重预留下显存），
        # 负数更会一路带进 max_model_len。写入侧由 UpdateModelRequest 的 gt=0 拦住，这里是兜底
        ctx = min(ctx, model_max_context)
    topology = Topology(tp=1, nnodes=1, replicas=1, max_model_len=ctx)
    vram = adapter.estimate_vram_gb(
        model_size_gb or 0.0,
        topology,
        model_type=model_type,
        dtype_bytes=kv_dtype_bytes,
        arch_meta=arch_meta,
        n_parallel=n_parallel,
    )
    image = image_catalog.pick_image(engine=engine_name, arch=arch) or ""
    return Recommendation(
        engine=engine_name,
        image=image,
        topology=topology.model_dump(),
        vram_per_gpu_gb=round(vram, 2),
        supported_parallel=list(getattr(adapter, "supported_parallel", []) or []),
    )


def should_derive_tp(
    gpu_selector: GpuSelector | None,
    topo: Topology,
    *,
    tp_explicit: bool,
    has_pool: bool,
) -> bool:
    """tp 自动推导的前置守卫（inspect 路由用）。

    推导域冻结不变量：自动推导只产出旧形状（同形单机单副本 pp=1）——pp>1 / 异构
    副本（copies 列表）是显式-only 词汇，机器不替用户选形状；无解就报无解，不做
    「回退改形状」。

    - 用户已显式指定 tp（含异构写法——各份自带模板）→ 不推导；
    - 手动选卡/选节点 → 不推导：用户已定卡集，按全集群推导只会误伤合法手选——如手选单张
      80GB 大卡却按集群其它小卡组推出 tp=2，被「需 2 张实际 1 张」误拒；
    - 单机单副本且 pp 未设：pp>1 时 need 是总卡数口径，不能全塞给 tp；多机/多副本
      拓扑同样不属单机推导域；
    - 集群无卡池（cluster_gpus 为空）→ 无从推导。
    """
    if tp_explicit or not has_pool:
        return False
    if not topo.is_homogeneous:
        return False
    if gpu_selector and (gpu_selector.gpu_uuids or gpu_selector.node_id or gpu_selector.node_ids):
        return False
    return (
        max(1, int(topo.pp or 1)) == 1 and max(1, int(topo.nnodes or 1)) == 1 and max(1, int(topo.replicas or 1)) == 1
    )


def feasible_tp_for_node(
    node_cards,
    *,
    required_vram_gb: float | None,
    shape: parallel.ParallelShape | None = None,
) -> int | None:
    """单节点上能跑该模型的最小合法 tp；跑不了返回 None。

    ``node_cards``：同一节点上的可选卡（带 ``total_vram_gb`` / ``available_vram_gb``）。

    **按平台默认口径判（整卡独占）**：每卡要预留 ``DEFAULT_GPU_MEMORY_UTILIZATION × total``
    （与 ``card_reserve`` 同源），它既得装得下「模型每卡需求 = required ÷ tp」，又不得
    超过卡的可用量。这就是「一张卡只推荐一个部署」：卡被占用超过 ``(1 − 0.9) × total`` 就不
    参与推荐，用户显式调低显存占比是另一回事——那只影响账本预留，不反过来改推荐口径。

    取**预留最大的前 tp 张**：按预留降序后第 tp 张就是这组的短板。
    """
    if not node_cards or required_vram_gb is None:
        return None

    def reserve_of(card) -> float:
        return card_reserve(card, None)

    # 先剔掉「可用量装不下自己这次预留」的卡——与调度 Filter 的同一条条件
    usable = sorted(
        (c for c in node_cards if reserve_fits(reserve_of(c), None, getattr(c, "available_vram_gb", 0.0) or 0.0)),
        key=reserve_of,
        reverse=True,
    )
    if not usable:
        return None  # 一张都承载不了（常见于卡普遍被部分占用）——不是「tp=1 可行」
    # 显存分档（与调度 pipeline._tier 同口径：round(total,1)，不用精确浮点——尾差会把
    # 同档卡判成不同档）。TP 组的卡须同档（跨档 TP 会被小卡拖死），逐档分组供下方取组
    tiers: dict[float, list] = {}
    for c in usable:
        tiers.setdefault(round(float(getattr(c, "total_vram_gb", 0) or 0), 1), []).append(c)
    for tp in parallel.suggested_tp_sizes(shape or parallel.ParallelShape(), max_tp=len(usable)):
        if tp == 1:
            weak = reserve_of(usable[0])  # 单卡无同档约束，取最强一张
        else:
            # 同档且够数的组里取最强一组，组内第 tp 弱的预留是该组的短板。不是只在
            # 「前 tp 张」上判——前 tp 张恰好跨档（如 48G+24G+24G 混排）不代表凑不出
            # 别的同档组合（3×24G），别把可行 tp 错杀成不可行
            weak = max(
                (
                    sorted((reserve_of(c) for c in group), reverse=True)[tp - 1]
                    for group in tiers.values()
                    if len(group) >= tp
                ),
                default=None,
            )
            if weak is None:
                continue  # 任何档都凑不出 tp 张同档卡
        if weak + 1e-6 >= required_vram_gb / tp:
            return tp
    return None


def derive_tp(
    eligible_cards,
    *,
    required_vram_gb: float | None,
    arch_order: list[str],
    shape: parallel.ParallelShape | None = None,
) -> int:
    """从可选卡推导能跑的最小 TP（单机 nnodes=1、pp=1 前置由调用方保证；适配器未测 pp/ep）。

    ``eligible_cards`` 任意带 ``arch / node_id / total_vram_gb / available_vram_gb`` 属性的对象
    （可选卡 = 在线 + 引擎支持 + 有匹配镜像 + 未被占用）。按 (arch, total_vram) 分组后按节点聚合，
    逐节点问 :func:`feasible_tp_for_node`。返回所有可行组中**最小**的 tp（1 表示有单卡可行的组
    ——能一张卡跑就不推两张）；完全无可行组时返回 1（此时模型在该集群放不下，交给后续校验报原因）。

    **合法**指 tp 能切开模型权重（``parallel.suggested_tp_sizes``：attention head 与 KV head
    两个整除约束）。旧口径是纯 ``ceil(required / 每卡可用)``，能推出 tp=3 这类引擎建层期必炸的
    值——显存算得下不等于引擎切得开。``shape`` 未知（模型没探测过架构形状）时合法集退化为
    ``1..组内卡数``。

    与 ``GpuScheduler.assign`` / ``recommend_placement`` 同口径（容量判据统一走
    ``card_reserve`` + ``reserve_fits``，平台默认口径 = 整卡 0.9），保证推导出的 tp 部署期
    真能选到卡。推导值是 best-effort 建议，实际落定仍以调度内核为准。
    ``arch_order`` 只决定优先尝试顺序，结果取 min 后顺序不再影响取值。
    """
    if not eligible_cards or required_vram_gb is None:
        return 1
    feasible: list[int] = []
    for arch in arch_order:
        by_total: dict[float, list] = defaultdict(list)
        for c in eligible_cards:
            if getattr(c, "arch", "") == arch:
                by_total[getattr(c, "total_vram_gb", 0.0) or 0.0].append(c)
        for cards in by_total.values():
            by_node: dict[str, list] = defaultdict(list)
            for c in cards:
                by_node[getattr(c, "node_id", "")].append(c)
            for node_cards in by_node.values():
                fit = feasible_tp_for_node(node_cards, required_vram_gb=required_vram_gb, shape=shape)
                if fit is not None:
                    feasible.append(fit)
    if not feasible:
        return 1
    return min(feasible)


def pick_cluster(
    cluster_gpu_pools: dict[int, list[GpuPoolEntry]],
    vram_per_gpu: float,
    tp: int,
    arch: str | None = None,
    *,
    shape: parallel.ParallelShape | None = None,
) -> ClusterPick | None:
    """纯函数：遍历集群 GPU 池，选最优集群。

    判定规则（粗判，不做精确调度——预留给真正创建部署时的 _plan）：
    - 过滤同 arch（如指定）+ 容量合格的 GPU（判据同 ``feasible_tp_for_node``：
      平台默认整卡预留装得下「模型每卡需求」且卡的实时可用装得下预留；
      tp>1 时每卡需求 = ``vram_per_gpu`` ÷ tp）
    - 单节点可行 = 有节点满足 ≥ tp 张合格 GPU → cross_node=False
    - 跨机可行 = 集群总合格 GPU ≥ tp（至少 tp 个不同节点各出 1 张）→ cross_node=True
    - 优先选单节点可行的集群；其次跨机；不可行 → None

    **``shape`` 给了且 ``tp == 1``**（= 并行度还没定，由自动推导决定）时改用「该集群能不能用某个
    合法 tp 装下」判，而不是拿 tp=1 的整卡需求（即 ``vram_per_gpu``）过滤：模型单卡装不下、同节点
    多张卡合起来装得下的集群，旧口径会整片漏掉——而 tp 自动推导存在的意义正是这种模型。用户已固定
    tp（``tp > 1``）时仍按传入 tp 判，尊重用户的形状选择。

    ``cluster_gpu_pools``: {cluster_id: [GpuPoolEntry, ...]}，调用方从 DB 装配。
    """
    auto_tp = max(1, int(tp or 1)) == 1 and shape is not None
    candidates: list[tuple[int, bool]] = []  # (cluster_id, cross_node)

    for cluster_id, gpus in cluster_gpu_pools.items():
        # arch 过滤 + 按节点分组：两条判定分支共用（省得各写一遍分组、日后改一处漏一处）
        by_node: dict[str, list[GpuPoolEntry]] = {}
        for g in gpus:
            if arch is None or g.arch == arch:
                by_node.setdefault(g.node_id, []).append(g)
        if not by_node:
            continue

        if auto_tp:
            if any(
                feasible_tp_for_node(cards, required_vram_gb=vram_per_gpu, shape=shape) for cards in by_node.values()
            ):
                candidates.append((cluster_id, False))
            continue

        # 再按容量过滤。tp 显式 >1 时每卡需求 = 整模型需求 ÷ tp（``vram_per_gpu`` 是 tp=1
        # 口径的模型总量，直接当单卡门槛会把「单卡装不下、切开装得下」的集群整片判死）。
        # 判据与 feasible_tp_for_node 同一条 reserve_fits（平台默认整卡预留）——被占用
        # 超过一成的卡不参与，与推荐口径一致。
        per_card_need = vram_per_gpu / max(1, int(tp or 1))
        qualified = {
            nid: [g for g in cards if reserve_fits(card_reserve(g, None), per_card_need, g.available_vram_gb)]
            for nid, cards in by_node.items()
        }
        qualified = {nid: cards for nid, cards in qualified.items() if cards}
        if not qualified:
            continue

        if max(len(cards) for cards in qualified.values()) >= tp:
            # 单节点可行
            candidates.append((cluster_id, False))
        elif len(qualified) >= tp:
            # 跨机可行（至少 tp 个不同节点各出 1 张）
            candidates.append((cluster_id, True))

    if not candidates:
        return None

    # 优先单节点（cross_node=False），取第一个
    single_node = [c for c in candidates if not c[1]]
    if single_node:
        return ClusterPick(cluster_id=single_node[0][0], cross_node=False)
    return ClusterPick(cluster_id=candidates[0][0], cross_node=True)


def pick_cluster_by_arch_order(
    cluster_gpu_pools: dict[int, list[GpuPoolEntry]],
    vram_per_gpu: float,
    tp: int | None,
    arch_order: list[str],
    *,
    shape: "parallel.ParallelShape | None" = None,
) -> ClusterPick | None:
    """按适配器架构试探序逐个回落挑集群（inspect 自动选集群的入口）。

    默认架构（如 vllm→nvidia）无合格卡时退回其它受支持架构再挑——只按默认架构
    过滤会把全 Ascend 平台整片滤掉，自动推荐永远静默为空。试探序来自
    ``adapter.arch_order``（默认优先、其余按声明序），与选卡内核的回落共用优先级。
    """
    for arch in arch_order:
        pick = pick_cluster(cluster_gpu_pools, vram_per_gpu, tp, arch=arch, shape=shape)
        if pick:
            return pick
    return None
