"""模型可部署性检测：显存分组匹配 + 引擎支持判定 + 逐层 offload / CPU 部署 per-node 评估。

被 inspect 端点（add-model 表单预填时告知「该集群是否支持」）与 create_deployment
预检（指定集群 + context_size）复用。纯逻辑（``_assess_groups`` / ``assess_engine_support`` /
``_assess_offload_nodes``）离线可单测；``_collect_gpu_resources`` / ``collect_node_resources`` 负责
DB + 实时显存装配。

## 显存匹配规则

- 分组键 ``(cluster_id, Gpu.arch, Gpu.total_vram_gb)`` —— 同集群 + 同架构 + 同显存大小
  （常见开源方案仅按厂商 runtime 分组，本实现更严：还要求同显存大小）。
- 所需显存 ``required_gb = 权重 + KV_cache(context_size)``，与 ``engine.base.estimate_required_vram``
  同源 → inspect/预检/部署口径一致（inspect 时 context 未知→KV=0 乐观裸权重；deploy 时加 KV）。
- 放行阈值：``required ≤ available × VRAM_UTILIZATION(0.9)``，留 10% headroom 吸收粗估误差 +
  运行期开销（OOM 无所谓，但别因估错拒长上下文）。
- 无实时数据（gpu_buffer 未起 / GPU 不在心跳）→ 按 ``total`` 乐观估（``realtime=False`` 标记）。

## 逐层 offload / CPU 部署（引擎声明 cpu 能力时）

per-node 评估：该 node 可用显存（``EngineAdapter.offload_gpu_archs`` 内的卡，如 llama.cpp
只算 nvidia）+ 可用 RAM。系统只在两端取解（全量上卡 / 纯 CPU）→ ``fit_offload`` = 两端
任一可行；VRAM 单独够 → ``fit_full_gpu``；无卡 node 也参与（纯 CPU）。其它引擎（未声明该
能力）走 GPU 分组口径
（同架构+同显存跨 node 求和、不判 RAM、允许多 node）。

## 引擎支持

粗粒度 ``type + format``（类别 ∈ ``supported_categories`` 且 格式 ∈ ``supported_formats``）。
不镜像引擎架构白名单——最终是否支持由引擎部署时裁判（既定决策）。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.engine import get_adapter, registered_engines
from llmpod.config.constants import ClusterStatus, GpuStatus, NodeStatus
from llmpod.config.enums import ModelType

from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager.deployment.resource import MemoryEstimate
from llmpod.apps.manager.deployment.resource_ledger import DEFAULT_GPU_MEMORY_UTILIZATION as VRAM_UTILIZATION
from llmpod.models.cluster import Cluster
from llmpod.models.gpu import Gpu
from llmpod.models.node import Node

log = logging.getLogger("llmpod.apps.manager.deployment.feasibility")

# 可用显存的放行阈值：required ≤ available × 0.9（留 10% headroom 吸收 KV 粗估误差 + 运行期开销）
# 与 resource_ledger.DEFAULT_GPU_MEMORY_UTILIZATION 同源——0.9 的 headroom 策略全仓唯一定义处，改一处即同步。
# （resource_ledger 用于乐观预留，feasibility 用于可部署性判定，口径须一致。）

# ============ 报告 schema ============


class GpuGroupAssessment(BaseModel):
    """一个同构 GPU 分组（同集群 + 同架构 + 同显存大小）的可用显存评估。"""

    model_config = ConfigDict(extra="ignore")

    cluster_id: int = Field(..., description="集群 ID")
    cluster_name: str = Field(..., description="集群名")
    arch: str = Field(..., description="GPU 架构（软件栈）：nvidia | cann | cpu")
    vram_gb: float = Field(..., description="单卡显存总量（GB，分组键，组内同构）")
    gpu_names: list[str] = Field(default_factory=list, description="去重 GPU 型号名（显示用）")
    online_gpus: int = Field(..., description="组内在线 GPU 数")
    total_vram_gb: float = Field(..., description="组内显存总量 Σ（GB）")
    available_vram_gb: float = Field(..., description="组内可用显存 Σ（GB）= Σ(total − 实时 used)")
    realtime: bool = Field(..., description="是否用到了实时 used_vram（False=按 total 乐观估）")
    can_fit: bool | None = Field(None, description="None=size 未知；否则 required ≤ available×0.9")
    deficit_gb: float | None = Field(None, description="缺口（required − available×0.9，<0 取 0）；size 未知为 None")


class EngineAssessment(BaseModel):
    """单引擎对当前模型的支持判定。"""

    model_config = ConfigDict(extra="ignore")

    engine: str = Field(..., description="引擎名：vllm | sglang | llamacpp")
    supported: bool | None = Field(
        None, description="True=类别+格式都支持；False=明确不支持；None=type/格式未定无法判定"
    )
    reasons: list[str] = Field(default_factory=list, description="不支持 / 未定的原因")
    supports_cpu_lane: bool = Field(False, description="该引擎是否声明 CPU / 内存部署能力（allow_cpu）；由适配器派生")


class NodeOffloadAssessment(BaseModel):
    """逐层 offload / CPU 部署：单 node 评估（可用显存 + RAM）。"""

    model_config = ConfigDict(extra="ignore")

    cluster_id: int = Field(..., description="集群 ID")
    cluster_name: str = Field(..., description="集群名")
    node_id: str = Field(..., description="节点 ID")
    hostname: str = Field(..., description="节点主机名")
    nvidia_gpus: int = Field(..., description="该 node 贡献显存的 nvidia GPU 数")
    avail_vram_gb: float = Field(..., description="该 node nvidia GPU 可用显存之和（GB）")
    avail_ram_gb: float = Field(..., description="该 node 可用内存（GB）")
    realtime_vram: bool = Field(..., description="VRAM 是否用了实时 used（False=按 total 乐观）")
    realtime_ram: bool = Field(..., description="RAM 是否用了实时 used（False=按 total 乐观）")
    combined_gb: float = Field(..., description="可用 VRAM + 可用 RAM（GB）")
    fit_full_gpu: bool | None = Field(
        None, description="None=size 未知；否则 required ≤ VRAM×0.9（全 GPU offload，快）"
    )
    fit_offload: bool | None = Field(
        None, description="None=size 未知；否则全量上卡或纯 CPU 任一可行（形状未知时退 required ≤ (VRAM+RAM)×0.9）"
    )
    deficit_gb: float | None = Field(None, description="缺口（required − combined×0.9，<0 取 0）；size 未知为 None")
    memory: "MemoryEstimate | None" = Field(
        None,
        description="逐层 offload 测算（架构形状已知时）：每档 (内存, 每卡显存) 需求与可行性；形状未知为 None",
    )


class FeasibilityReport(BaseModel):
    """可部署性检测报告。"""

    model_config = ConfigDict(extra="ignore")

    model_size_gb: float | None = Field(None, description="模型权重大小（GB）")
    context_size: int | None = Field(None, description="最大上下文长度（tokens），KV cache 估算用")
    required_vram_gb: float | None = Field(None, description="所需显存 = 权重 + KV_cache(context)；size 未知为 None")
    gpu_groups: list[GpuGroupAssessment] = Field(default_factory=list, description="同构 GPU 分组评估（GPU 分组路径）")
    engines: list[EngineAssessment] = Field(default_factory=list, description="各引擎对模型的支持判定")
    deployable_clusters: list[int] = Field(
        default_factory=list, description="可部署的集群 ID（有 fit 分组且其 arch 被某 supported 引擎接受）"
    )
    offload_nodes: list[NodeOffloadAssessment] = Field(
        default_factory=list,
        description="逐层 offload / CPU 部署路径专用：per-node 评估（可用显存 + RAM，单 node）；非该路径为空",
    )
    deployable: bool = Field(..., description="整体是否可部署")
    message: str | None = Field(None, description="人读总结")


# ============ 纯逻辑（离线可单测） ============


@dataclass
class GPUResource:
    """单个在线 GPU 的可用资源（DB + 实时显存装配后喂纯函数）。"""

    cluster_id: int
    cluster_name: str
    arch: str
    total_vram_gb: float
    name: str
    used_vram_gb: float | None  # None = 无实时数据


@dataclass
class NodeResource:
    """单个在线 node 的可用资源（GGUF offload 评估用）。"""

    cluster_id: int
    cluster_name: str
    node_id: str
    hostname: str
    nvidia_gpus: int
    avail_vram_gb: float  # 该 node nvidia GPU 可用显存之和
    max_card_vram_gb: float  # 单卡可用显存的最大值（逐卡容量判定的口径：逐层表按 tp×pp 均摊）
    avail_ram_gb: float  # 该 node 可用内存
    memory_gb: float  # 该 node 总内存（账本口径的基数：get_available_ram = 总内存×利用率上限 − 预留）
    realtime_vram: bool
    realtime_ram: bool


def _fit(required: float | None, cap: float) -> bool | None:
    """required ≤ cap（含 1e-9 浮点容差）；required 未知 → None（跳过显存判定）。

    放行阈值逻辑的唯一定义处——``_assess_groups`` 与 ``_assess_offload_nodes`` 共用，
    避免 ``required <= cap + 1e-9`` + None 守卫在多处复制漂移。
    """
    return None if required is None else required <= cap + 1e-9


def _deficit(required: float | None, cap: float) -> float | None:
    """缺口（required − cap，<0 取 0，GB）；required 未知 → None。与 ``_fit`` 同源。"""
    return None if required is None else round(max(0.0, required - cap), 2)


def _assess_groups(resources: list[GPUResource], required_gb: float | None) -> list[GpuGroupAssessment]:
    """按 (cluster_id, arch, total_vram_gb) 分组，组内 Σ total / Σ available / can_fit / deficit。

    can_fit: required ≤ available × VRAM_UTILIZATION（0.9，留 headroom）。
    """
    buckets: dict[tuple[int, str, float], list[GPUResource]] = defaultdict(list)
    for r in resources:
        # total_vram_gb 取整到 0.001 GB 作分组键，避免浮点抖动拆组
        buckets[(r.cluster_id, r.arch, round(r.total_vram_gb, 3))].append(r)

    groups: list[GpuGroupAssessment] = []
    for (cid, arch, vram), members in buckets.items():
        total = sum(m.total_vram_gb for m in members)
        realtime = all(m.used_vram_gb is not None for m in members)
        avail = sum(max(0.0, m.total_vram_gb - (m.used_vram_gb or 0.0)) for m in members)
        cap = avail * VRAM_UTILIZATION  # 放行容量（留 headroom）
        groups.append(
            GpuGroupAssessment(
                cluster_id=cid,
                cluster_name=members[0].cluster_name,
                arch=arch,
                vram_gb=vram,
                gpu_names=sorted({m.name for m in members if m.name}),
                online_gpus=len(members),
                total_vram_gb=round(total, 2),
                available_vram_gb=round(avail, 2),
                realtime=realtime,
                can_fit=_fit(required_gb, cap),
                deficit_gb=_deficit(required_gb, cap),
            )
        )
    groups.sort(key=lambda g: (g.cluster_id, g.arch, -g.vram_gb))
    return groups


def assess_engine_support(model_type: str | None, model_format: str | None) -> list[EngineAssessment]:
    """遍历注册引擎，判 model_type ∈ supported_categories 且 model_format ∈ supported_formats。

    公共入口：部署预检与「引擎列表按模型判定」（前端 Step1 置灰）共用同一规则，避免两处漂移。
    """
    out: list[EngineAssessment] = []
    for engine in registered_engines():
        adapter = get_adapter(engine)
        reasons: list[str] = []

        type_ok: bool | None
        if model_type is None:
            type_ok = None
        elif model_type in adapter.supported_categories:
            type_ok = True
        else:
            type_ok = False
            reasons.append(f"类别 {model_type} 不在支持列表 {adapter.supported_categories}")

        format_ok: bool | None
        if model_format is None:
            format_ok = None
        elif model_format in adapter.supported_formats:
            format_ok = True
        else:
            format_ok = False
            reasons.append(f"格式 {model_format} 不在支持列表 {adapter.supported_formats}")

        if type_ok is False or format_ok is False:
            supported = False
        elif type_ok is None or format_ok is None:
            supported = None
            reasons.insert(0, "type/格式 未定（非 HF 源或探测不全），无法判定")
        else:
            supported = True

        out.append(
            EngineAssessment(
                engine=engine,
                supported=supported,
                reasons=reasons,
                supports_cpu_lane=adapter.supports_cpu_lane(),
            )
        )
    return out


def _supported_archs(engines: list[EngineAssessment]) -> set[str]:
    """收集「明确支持模型」的引擎所接受的 GPU arch 集合。

    ``get_adapter`` 每次新建无状态实例——这里对 supported 引擎各调一次（与 ``assess_engine_support``
    的调用重复），单次构造开销极小（无 ``__init__`` 副作用），不做缓存。
    """
    archs: set[str] = set()
    for e in engines:
        if e.supported is True:
            archs.update(get_adapter(e.engine).supported_gpu_archs)
    return archs


def _deployable_clusters(
    groups: list[GpuGroupAssessment], engines: list[EngineAssessment], required_gb: float | None
) -> list[int]:
    """可部署集群（非 GGUF）：有 can_fit 分组 且 其 arch ∈ 某 supported 引擎的 supported_gpu_archs。"""
    if required_gb is None:
        return []
    archs = _supported_archs(engines)
    if not archs:
        return []
    out: list[int] = []
    for g in groups:
        if g.can_fit and g.arch in archs and g.cluster_id not in out:
            out.append(g.cluster_id)
    return out


def _cpu_deployable_engines(engines: list[EngineAssessment]) -> list[EngineAssessment]:
    """声明 CPU 部署能力**且支持该模型**（type/format）的引擎。

    deployable 结论门与总结门共用同一谓词——两处手写会漂移出「总结说可部署、
    deployable 却是 False」的自相矛盾（守卫 ``supported is True`` 非仅「已注册」，
    避免把声明了 CPU 部署但服务不了该类别的引擎误判）。
    """
    return [e for e in engines if e.supports_cpu_lane and e.supported is True]


def _deployable_offload_clusters(
    offload_nodes: list[NodeOffloadAssessment], engines: list[EngineAssessment], required_gb: float | None
) -> list[int]:
    """可部署集群（逐层 offload / CPU 部署路径）：有 fit_offload 的 node 且存在 CPU 可部署引擎。"""
    if required_gb is None:
        return []
    if not _cpu_deployable_engines(engines):
        return []
    return sorted({n.cluster_id for n in offload_nodes if n.fit_offload})


def _summarize(
    groups: list[GpuGroupAssessment],
    engines: list[EngineAssessment],
    required_gb: float | None,
    deployable_clusters: list[int],
) -> str | None:
    if not groups:
        return "无在线可用节点"
    if required_gb is None:
        return "模型权重大小未知，仅做引擎支持判定，显存匹配已跳过"
    supported_engines = [e.engine for e in engines if e.supported is True]
    if not supported_engines:
        return f"无引擎支持该模型（type/format），显存匹配参考用；需 {required_gb} GB"
    if not deployable_clusters:
        can_fit_clusters = sorted({g.cluster_id for g in groups if g.can_fit})
        if can_fit_clusters:
            return f"集群 {can_fit_clusters} 显存够但 GPU 架构无引擎支持；其余集群显存不足，需 {required_gb} GB"
        return f"显存不足：所有集群同构分组可用×0.9 < 所需 {required_gb} GB"
    return f"可在集群 {deployable_clusters} 部署（所需 {required_gb} GB）"


def _assess_offload_nodes(
    resources: list[NodeResource],
    required_gb: float | None,
    *,
    weight_gb: float | None = None,
    kv_gb: float = 0.0,
    total_layers: int | None = None,
    allow_cpu_lane: bool = False,
    parallel: int = 1,
) -> list[NodeOffloadAssessment]:
    """per-node 评估逐层 offload / CPU 部署；无卡 node 也参与。

    架构形状已知（``total_layers`` 有值）→ 走逐层 offload 模型：该节点能否放下、
    推荐几层上 GPU、KV 落哪一池都由 :func:`resource.estimate_memory_requirements` 定，
    ``fit_full_gpu`` = 全量上卡档可行、``fit_offload`` = 两端任一可行（系统只取
    「全量上卡 / 纯 CPU」两端，中间混合档只进逐层表不参与取解，见
    resource.estimate_memory_requirements）。
    形状未知 → 退回旧的粗估（两端分别判，同上口径），保证老模型不因缺元数据变不可部署。

    两个口径与求解路径对齐（否则诊断结论与调度结果相反）：

    - ``allow_cpu_lane``：纯 CPU 档**算不算能部署**取决于请求是否放行该部署
      （``allow_cpu`` / 显式 ``cpu_only``）。逐层表始终算全（表是「能不能放」的事实），
      但只有放行时纯 CPU 端才计入 ``fit_offload``——否则 ``allow_cpu=False`` 的请求会在
      「只有纯 CPU 档可行」的集群上报可部署，实际选卡时被调度器拒。
    - ``parallel`` 与 ``r.max_card_vram_gb``：逐层表的 ``vram_gb`` 是**每卡**需求
      （按 tp×pp 均摊），故显存预算必须用**单卡最大可用**而非该节点显存之和，否则
      「2×24GB 节点 + tp=1 + 30GB 模型」会被判可行（30 ≤ 44×0.9），实际一块卡放不下。
    """
    out: list[NodeOffloadAssessment] = []
    for r in resources:
        combined = r.avail_vram_gb + r.avail_ram_gb
        vram_cap = r.max_card_vram_gb * VRAM_UTILIZATION
        memory = None
        if total_layers and weight_gb:
            memory = resource.estimate_memory_requirements(
                weight_gb=weight_gb,
                kv_gb=kv_gb,
                total_layers=total_layers,
                vram_avail_gb=r.max_card_vram_gb,
                ram_avail_gb=r.avail_ram_gb,
                parallel=parallel,
                # 逐层表是「能不能放」的事实（每档都列），部署授权由 allow_cpu_lane 决定
                allow_cpu=True,
            )
        if memory is not None:
            full_tier = memory.tiers[-1]
            fit_full_gpu = full_tier.feasible
            # 系统只在两端取解（全量上卡 / 纯 CPU）——中间档只进表不取解，故「能不能部署」
            # 也只认两端；纯 CPU 端还要请求放行该部署（见 docstring）
            fit_offload = (allow_cpu_lane and memory.tiers[0].feasible) or full_tier.feasible
            # 缺口与 resource 的 tier 判定同口径（显存=单卡池、内存=RAM 池，勿用两池合并值）：
            # 两端解各算「最紧子项差值」，部署缺口取更近一端；任一端可行 ⇒ 0。RAM 预算用
            # resource 的通用 headroom（与 estimate_memory_requirements 的内存判定同源）
            ram_cap = r.avail_ram_gb * resource.DEFAULT_HEADROOM
            gap_full = max(full_tier.vram_gb - vram_cap, full_tier.ram_gb - ram_cap)
            gap_cpu = memory.tiers[0].ram_gb - ram_cap
            deficit_gb = round(max(0.0, min(gap_full, gap_cpu)), 2)
        else:
            # 形状未知（缺层数/权重）：退回粗估。口径与已知形状的分支同语义——「能不能部署」
            # 只认真实存在的两端形态：全量上卡 = 单卡显存池、纯 CPU = 内存池（且须请求放行，
            # 未授权的 CPU 不算能部署）。「显存+内存合并池」判定的是真实系统里不存在的混合
            # 形态（真实调度只有全量上卡/纯 CPU 两解），会给出预检绿灯、创建必败的结论
            fit_full_gpu = _fit(required_gb, vram_cap)
            cpu_cap = r.avail_ram_gb * VRAM_UTILIZATION
            fit_cpu = _fit(required_gb, cpu_cap) if allow_cpu_lane else None
            fit_offload = fit_full_gpu if fit_cpu is None else (fit_full_gpu or fit_cpu)
            best_cap = max(vram_cap, cpu_cap) if allow_cpu_lane else vram_cap
            deficit_gb = _deficit(required_gb, best_cap)
        out.append(
            NodeOffloadAssessment(
                cluster_id=r.cluster_id,
                cluster_name=r.cluster_name,
                node_id=r.node_id,
                hostname=r.hostname,
                nvidia_gpus=r.nvidia_gpus,
                avail_vram_gb=round(r.avail_vram_gb, 2),
                avail_ram_gb=round(r.avail_ram_gb, 2),
                realtime_vram=r.realtime_vram,
                realtime_ram=r.realtime_ram,
                combined_gb=round(combined, 2),
                fit_full_gpu=fit_full_gpu,
                fit_offload=fit_offload,
                deficit_gb=deficit_gb,
                memory=memory,
            )
        )
    out.sort(key=lambda n: (-n.combined_gb, n.cluster_id, n.node_id))
    return out


def _summarize_offload(
    offload_nodes: list[NodeOffloadAssessment],
    engines: list[EngineAssessment],
    required_gb: float | None,
    deployable_clusters: list[int],
) -> str | None:
    """逐层 offload / CPU 部署视图的人读总结。结论门与 ``_deployable_offload_clusters``
    同谓词（``_cpu_deployable_engines``），否则 rerank+gguf 这类组合会出现「总结说可
    部署、deployable 却是 False」的自相矛盾。"""
    if not offload_nodes:
        return "无在线可用节点"
    if required_gb is None:
        return "模型权重大小未知，仅做引擎支持判定，显存/内存匹配已跳过"
    lanes = [e for e in engines if e.supports_cpu_lane]
    if not lanes:
        return f"无支持 CPU / 内存部署的引擎，无法判定逐层 offload 部署；需 {required_gb} GB"
    if not _cpu_deployable_engines(engines):
        # supported=None（type/format 未定）≠ 不支持：引擎 reasons 会写「未定，无法判定」，
        # 总结跟着说「均不支持」会把用户引向换模型，而真相是补齐 model_type 即可
        if all(e.supported is None for e in lanes):
            return (
                f"支持 CPU / 内存部署的引擎尚未判定对该模型的支持（type/format 元数据不全），"
                f"补齐后可判定；需 {required_gb} GB"
            )
        return f"支持 CPU / 内存部署的引擎均不支持该模型（type/format）；需 {required_gb} GB"
    fit = [n for n in offload_nodes if n.fit_offload]
    if fit:
        best = fit[0]
        full_gpu = [n for n in fit if n.fit_full_gpu]
        full_hint = f"；其中 {len(full_gpu)} 台可全 GPU offload（无需走 CPU）" if full_gpu else "（均需走 CPU）"
        return (
            f"可在 {len(fit)} 台 node 部署（逐层 offload，所需 {required_gb} GB）；"
            f"推荐 {best.hostname or best.node_id}（可用 {best.combined_gb} GB = {best.avail_vram_gb} 显存 + {best.avail_ram_gb} 内存）"
            f"{full_hint}"
        )
    top = offload_nodes[0]
    return (
        f"两端部署形态均装不下（全量上卡=单卡显存 / 纯 CPU=节点内存）：最大 node "
        f"{top.hostname or top.node_id} 可用 {top.avail_vram_gb} GB 显存 + {top.avail_ram_gb} GB 内存，"
        f"需 {required_gb} GB，缺口 {top.deficit_gb} GB"
    )


# ============ 装配（DB + 实时显存） ============


async def _collect_gpu_resources(
    db: AsyncSession, cluster_id: int | None = None, *, used_vram: dict[str, float] | None = None
) -> list[GPUResource]:
    """join 查在线 GPU + 其节点集群归属；配对实时 used_vram_gb。cluster_id 非空则只查该集群。

    Cluster.status 须 active——停用/下线集群的 GPU 不进全局 inspect 扫描，避免报「可在
    已下线集群部署」。Node/Gpu 仍各自判 online（集群停用时其下 node 常仍在线）。

    ``used_vram``：调用方已取好的实时用量；None → 本函数自取。该缓冲是全平台读数，同一
    请求里另一处也要用时透传即可，省一遍全量字典重建（与 :func:`collect_node_resources` 同）。
    """
    stmt = (
        select(Gpu, Node.cluster_id, Cluster.name)
        .join(Node, Node.id == Gpu.node_id)
        .join(Cluster, Cluster.id == Node.cluster_id)
        .where(Gpu.status == GpuStatus.ONLINE, Node.status == NodeStatus.ONLINE, Cluster.status == ClusterStatus.ACTIVE)
    )
    if cluster_id is not None:
        stmt = stmt.where(Node.cluster_id == cluster_id)
    rows = (await db.execute(stmt)).all()

    used_by_uuid = load_used_vram_by_uuid() if used_vram is None else used_vram

    resources: list[GPUResource] = []
    for g, cid, cluster_name in rows:
        resources.append(
            GPUResource(
                cluster_id=cid,
                cluster_name=cluster_name or "",
                arch=g.arch or "",
                total_vram_gb=g.total_vram_gb or 0.0,
                name=g.name or "",
                used_vram_gb=used_by_uuid.get(g.uuid),
            )
        )
    return resources


async def collect_node_resources(
    db: AsyncSession,
    cluster_id: int | None = None,
    *,
    gpu_archs: Sequence[str] | None = None,
    used_vram: dict[str, float] | None = None,
) -> list[NodeResource]:
    """在线 node + 可用显存（``gpu_archs`` 内的卡）+ 可用内存。cluster_id 非空则只查该集群。

    ``gpu_archs``：计入显存的卡架构（来自 :meth:`EngineAdapter.offload_gpu_archs`——
    CPU/offload 部署只算引擎真能用的卡，如 llama.cpp 只算 nvidia）；None = 不过滤架构。
    ``used_vram``：调用方已取好的实时用量（``load_used_vram_by_uuid``）；None → 本函数自取。
    该缓冲是全平台读数，同一请求里另一处也要用时透传即可，省一遍全量字典重建。

    注：node 查询与 GPU 查询无数据依赖，但 SQLAlchemy async session 单连接，``asyncio.gather``
    并发 execute 不安全（连接/状态可能交错），故顺序执行；两查询均过滤 online 行，开销小。
    """
    nstmt = (
        select(Node, Cluster.name)
        .join(Cluster, Cluster.id == Node.cluster_id)
        .where(Node.status == NodeStatus.ONLINE, Cluster.status == ClusterStatus.ACTIVE)
    )
    if cluster_id is not None:
        nstmt = nstmt.where(Node.cluster_id == cluster_id)
    node_rows = (await db.execute(nstmt)).all()
    nodes_by_id: dict[
        str, tuple[int, str, str, float]
    ] = {}  # node_id -> (cluster_id, cluster_name, hostname, memory_gb)
    for n, cluster_name in node_rows:
        nodes_by_id[n.id] = (n.cluster_id, cluster_name or "", n.hostname or "", n.memory_gb or 0.0)

    gpus_by_node: dict[str, list[Gpu]] = {}
    if nodes_by_id:
        # 按节点过滤：本集群之外的在线卡不该被水合出来再丢掉（多集群平台上与规模成正比）
        gstmt = select(Gpu).where(Gpu.status == GpuStatus.ONLINE, Gpu.node_id.in_(list(nodes_by_id)))
        if gpu_archs:
            gstmt = gstmt.where(Gpu.arch.in_(list(gpu_archs)))
        for g in (await db.execute(gstmt)).scalars():
            gpus_by_node.setdefault(g.node_id, []).append(g)

    used_by_uuid = load_used_vram_by_uuid() if used_vram is None else used_vram
    ram_total_by_node, ram_used_by_node = load_ram_by_node()

    resources: list[NodeResource] = []
    for nid, (cid, cname, hostname, mem_total_db) in nodes_by_id.items():
        gpus = gpus_by_node.get(nid, [])
        realtime_vram = True
        avail_vram = 0.0
        max_card_vram = 0.0
        for g in gpus:
            used = used_by_uuid.get(g.uuid)
            if used is None:
                realtime_vram = False
                used = 0.0
            card_avail = max(0.0, (g.total_vram_gb or 0.0) - used)
            avail_vram += card_avail
            max_card_vram = max(max_card_vram, card_avail)

        total, realtime_ram = node_ram_total(ram_total_by_node, nid, mem_total_db)
        buf_used = ram_used_by_node.get(nid)
        used = (buf_used if buf_used is not None else 0.0) if realtime_ram else 0.0
        avail_ram = max(0.0, total - used)

        resources.append(
            NodeResource(
                cluster_id=cid,
                cluster_name=cname,
                node_id=nid,
                hostname=hostname,
                nvidia_gpus=len(gpus),
                avail_vram_gb=avail_vram,
                max_card_vram_gb=max_card_vram,
                avail_ram_gb=avail_ram,
                memory_gb=total,
                realtime_vram=realtime_vram,
                realtime_ram=realtime_ram,
            )
        )
    return resources


def load_used_vram_by_uuid() -> dict[str, float]:
    """从 gpu_buffer 取实时 used_vram（按 uuid）；buffer 未起/取数失败返回空 dict（乐观按 total）。"""
    out: dict[str, float] = {}
    try:
        from llmpod.apps.manager.app import gpu_buffer

        if gpu_buffer is not None:
            for _nid, infos in gpu_buffer.get_all().items():
                for info in infos:
                    out[info.uuid] = info.used_vram_gb
    except Exception as e:
        log.warning("feasibility: gpu_buffer 取数失败，按 total 乐观估: %s", e)
    return out


def load_ram_by_node() -> tuple[dict[str, float], dict[str, float]]:
    """从 ``system_buffer`` 取实时 RAM（总量/已用，按 node_id）；未起/失败返回两个空 dict。

    **心跳 RAM 的唯一读取点**：节点总内存该取实时值还是回退 DB ``Node.memory_gb``，
    调用的每一处（预检的节点资源、编排的节点候选）都从这里拿，避免两套「节点可用内存」。
    调用方自行处理 0 值（心跳漏 memory_gb 时存 0，不能当真实总量覆盖 DB）。
    """
    total: dict[str, float] = {}
    used: dict[str, float] = {}
    try:
        from llmpod.apps.manager.app import system_buffer

        if system_buffer is not None:
            for nid, st in system_buffer.get_all().items():
                total[nid] = st.memory_gb
                used[nid] = st.memory_used_gb
    except Exception as e:
        log.warning("feasibility: system_buffer 取数失败，按 total 乐观估: %s", e)
    return total, used


def node_ram_total(ram_total_by_node: dict[str, float], node_id: str, mem_total_db: float | None) -> tuple[float, bool]:
    """节点总内存 ``(GiB, 是否实时值)``：心跳实时值优先，缺失/非正回退 DB ``Node.memory_gb``。

    ``system_buffer`` 心跳漏 memory_gb 时存 0.0（``NodeReport.memory_gb`` 默认 0.0）；0.0
    不能当真实总量覆盖 DB——否则可用 RAM 归零、GGUF 误拒，还会谎报「有实时数据」。预检的
    节点资源与 CPU 部署候选共用这一条规则。
    """
    buf_total = ram_total_by_node.get(node_id)
    if buf_total is not None and buf_total > 0:
        return float(buf_total), True
    return float(mem_total_db or 0.0), False


async def assess_feasibility(
    db: AsyncSession,
    *,
    model_size_gb: float | None,
    model_type: str | None,
    model_format: str | None,
    cluster_id: int | None = None,
    context_size: int | None = None,
    kv_dtype_bytes: float | None = None,
    arch_meta: resource.KVArchMeta | None = None,
    n_parallel: int | None = None,
    total_layers: int | None = None,
    node_resources: list[NodeResource] | None = None,
    used_vram: dict[str, float] | None = None,
    allow_cpu: bool = False,
    parallel: int = 1,
) -> FeasibilityReport:
    """可部署性检测（可复用）：显存分组匹配 + 引擎支持 + GGUF offload。

    - ``model_size_gb``/``model_type``/``model_format`` 任一为 None 时对应检测降级（不抛）；
    - ``cluster_id=None``（inspect 全局扫描）/ 非 None（create_deployment 指定集群预检）；
    - ``context_size=None``（inspect 未知 KV）/ 非 None（deploy 加 KV，避免长上下文跑起来被拒）；
    - ``kv_dtype_bytes``/``arch_meta``/``n_parallel``：KV 精度档与架构形状（真公式输入）；
    - ``total_layers`` 已知时 GGUF 分支走逐层 offload 模型，否则退回显存+内存单一池粗估；
    - ``node_resources`` 非空时直接用调用方已采集的节点资源（同一请求里 inspect 还要拿它算部署形态，
      避免重复查库）；None → 本函数自行采集；
    - ``used_vram``：调用方已取好的实时显存用量（GPU 分组分支用）；None → 自行取；
    - ``allow_cpu``（请求是否放行纯 CPU 部署）+ ``parallel``（该份 tp×pp）：逐层 offload 判定
      的部署授权与逐卡口径，须与调度路径同口径，见 :func:`_assess_offload_nodes`；
    - 读 DB（在线 GPU + 集群，按 cluster_id 过滤）+ ``gpu_buffer``/``system_buffer``（实时）；
    - 返回 ``FeasibilityReport``，供 inspect 响应 / create_deployment 预检消费。
    """
    engines = assess_engine_support(model_type, model_format)
    # embedding/rerank 类别无自回归 KV cache，估算不含 KV 项（类别级判定，与引擎无关）；
    # model_type 未知 → 保守按有 KV 估。KV 走全仓唯一入口 ``resource.kv_cache_gb``，
    # required = 权重 + KV 前向合成（不从 estimate_required_vram 反减——反减会跟着它
    # 将来加项而静默错位，把别的项算进 KV 池）。
    kv_gb = resource.kv_cache_gb(
        model_size_gb,
        context_size,
        has_kv_cache=ModelType.has_kv_cache(model_type),
        dtype_bytes=kv_dtype_bytes if kv_dtype_bytes is not None else resource.DEFAULT_KV_DTYPE_BYTES,
        arch_meta=arch_meta,
        n_parallel=n_parallel,
    )
    required_gb = None if model_size_gb is None else round(model_size_gb + kv_gb, 2)

    # 分支选择按引擎能力（不写引擎名/格式字面量）：存在「声明 CPU / 内存部署能力且格式可服务」
    # 的引擎时走逐层 offload 模型（权重可拆到内存、KV 只落一边池子）；否则走 GPU 分组口径。
    offload_adapter = None
    for e in engines:
        if not e.supports_cpu_lane or not model_format:
            continue
        adapter = get_adapter(e.engine)
        if model_format in adapter.supported_formats:
            offload_adapter = adapter
            break

    # 分支只产出「本路径特有字段」与可部署集群列表；report 装配在底部统一，避免两份 FeasibilityReport 字段漂移
    # 两分支都守 ``any(e.supported is True)`` —— 否则无引擎能 serve 该类别时不应判 deployable
    # （GGUF 分支此前漏此守卫，rerank/image 等 llamacpp 不支持的类别曾被误判可部署）。
    if offload_adapter is not None:
        if node_resources is None:
            # used_vram 透传（与 GPU 分支同法）：同一请求里已取的实时用量直接复用，
            # 缺省不传会让 collect_node_resources 重建一遍全平台显存快照
            node_resources = await collect_node_resources(
                db, cluster_id, gpu_archs=offload_adapter.offload_gpu_archs(), used_vram=used_vram
            )
        offload_nodes = _assess_offload_nodes(
            node_resources,
            required_gb,
            weight_gb=model_size_gb,
            kv_gb=kv_gb,
            total_layers=total_layers,
            allow_cpu_lane=allow_cpu,
            parallel=parallel,
        )
        gpu_groups: list[GpuGroupAssessment] = []
        dep_clusters = _deployable_offload_clusters(offload_nodes, engines, required_gb)
        message = _summarize_offload(offload_nodes, engines, required_gb, dep_clusters)
    else:
        gpu_groups = _assess_groups(await _collect_gpu_resources(db, cluster_id, used_vram=used_vram), required_gb)
        offload_nodes: list[NodeOffloadAssessment] = []
        dep_clusters = _deployable_clusters(gpu_groups, engines, required_gb)
        message = _summarize(gpu_groups, engines, required_gb, dep_clusters)

    # 可部署 = 有引擎真能服务该类别 且 本路径解出至少一个集群。两分支同一守卫，收敛成
    # 一处——此前 GGUF 分支漏过它（rerank 等类别被误判可部署），分头各写就是漏改的温床
    deployable = required_gb is not None and any(e.supported is True for e in engines) and len(dep_clusters) > 0

    return FeasibilityReport(
        model_size_gb=model_size_gb,
        context_size=context_size,
        required_vram_gb=required_gb,
        gpu_groups=gpu_groups,
        engines=engines,
        deployable_clusters=dep_clusters,
        offload_nodes=offload_nodes,
        deployable=deployable,
        message=message,
    )
