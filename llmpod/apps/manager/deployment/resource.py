"""资源测算：KV cache 真公式 + 逐层 offload 表 + 显存/内存双池求解。

权重可按层拆分（一部分上 GPU、其余留内存），KV cache 不可拆且随权重落点（纯 CPU 档在
主机内存，其余在显存——KV 落点不可配），所以「能不能跑」由三个约束同时决定：

1. 总权重 ``W ≤ VRAM + RAM``（必要，权重可拆）；
2. 显存预算 ``o·W + K_vram + B ≤ VRAM × headroom``；
3. 内存预算 ``(1−o)·W + K_ram + OS ≤ RAM × headroom``。

``o = offload_layers / total_layers`` 是自由旋钮。逐层表给出每一档的 (内存需求, 每卡显存
需求) 与是否可行，**求解只在两端取点**（显存满上 / 纯 CPU，见
:func:`estimate_memory_requirements`）——中间混合档只作为事实列在表里供前端展示，
系统不自动选。调度、预检、前端资源面板共用这张表，避免三处各算一遍。

KV 占用只有一个计算入口（:func:`kv_cache_gb`）：架构元数据齐备走真公式，缺失时退回旧的
「权重的 10% @8K」启发式，两条分支都按 KV 精度档的 ``dtype_bytes`` 缩放，杜绝两套口径漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from llmpod.apps.manager.deployment.parallel import positive_int

# —— 启发式兜底（架构元数据缺失时用；数值与历史口径一致，勿随意改）——
KV_BASE_RATIO = 0.1
KV_BASE_CONTEXT = 8192

# 未指定 KV 精度档时的元素字节数（f16/bf16 满额）
DEFAULT_KV_DTYPE_BYTES = 2.0
# KV 真公式的并发序列数默认值：平台按「能不能装下一条满长请求」判定可部署性，与引擎
# 启动校验同口径（vLLM 只要求 KV 池至少容纳一条 max_model_len）。要按更高并发估峰值
# 时由 kv_cache.n_parallel 显式覆盖。
DEFAULT_N_PARALLEL = 1
# 单池可用上限（与账本 DEFAULT_GPU_MEMORY_UTILIZATION 同源，留 10% 给框架/激活/页缓存）
DEFAULT_HEADROOM = 0.9

# 场景名（也是偏好取值）：显存满上 / 显存主力 / 内存主力 / 纯 CPU
SCENARIO_GPU_ONLY = "gpu_only"
SCENARIO_GPU_MAIN = "gpu_main"
SCENARIO_CPU_MAIN = "cpu_main"
SCENARIO_CPU_ONLY = "cpu_only"
SCENARIOS: tuple[str, ...] = (SCENARIO_GPU_ONLY, SCENARIO_GPU_MAIN, SCENARIO_CPU_MAIN, SCENARIO_CPU_ONLY)
# 取向 CPU 的偏好对（_resolve_tier 的 prefer_cpu 判据）；取向 CPU 时解恒纯 CPU 档，
# tp 推导对它只会造出 CPU 通道装不下的形状（见 inspect._derive_tp 的跳过分支）
CPU_PREFERENCES: tuple[str, ...] = (SCENARIO_CPU_MAIN, SCENARIO_CPU_ONLY)
PREF_AUTO = "auto"
PREFERENCES: tuple[str, ...] = (PREF_AUTO, *SCENARIOS)


@dataclass(frozen=True)
class KVArchMeta:
    """KV 真公式所需的架构参数（任一缺失即整组不可用，退回启发式）。

    只认 MHA/GQA 口径（``num_kv_heads`` + ``head_dim``，每 token 每层存 K 与 V 两份）；
    MLA 类架构（DeepSeek 压缩 KV）没有独立口径，落到 :func:`kv_cache_gb` 的启发式。
    """

    num_layers: int
    num_kv_heads: int
    head_dim: int
    # 有 KV 的层数：混合线性注意力模型（如 Qwen3.5 的 GDN，4 层里只有 1 层全注意力）
    # 只有部分层存 KV，按全层算会高估数倍；None/非法值回退 num_layers（非混合模型两者相等）
    kv_layers: int | None = None

    @classmethod
    def from_model(cls, model) -> "KVArchMeta | None":
        """从 Model 行取架构字段；前三项缺失/非正数 → None（调用方退启发式）。"""
        num_layers = positive_int(getattr(model, "num_layers", None))
        num_kv_heads = positive_int(getattr(model, "num_kv_heads", None))
        head_dim = positive_int(getattr(model, "head_dim", None))
        if num_layers is None or num_kv_heads is None or head_dim is None:
            return None
        return cls(num_layers, num_kv_heads, head_dim, positive_int(getattr(model, "kv_layers", None)))


def kv_cache_gb(
    weight_gb: float | None,
    context_size: int | None,
    *,
    has_kv_cache: bool = True,
    dtype_bytes: float = DEFAULT_KV_DTYPE_BYTES,
    arch_meta: KVArchMeta | None = None,
    n_parallel: int | None = None,
) -> float:
    """KV cache 占用（GiB）。全仓唯一的 KV 计算入口。

    - 无自回归 KV cache（embedding/rerank）或上下文未知 → 0；
    - ``arch_meta`` 齐备 → 真公式
      ``2 × n_parallel × ctx × kv_layers × kv_heads × head_dim × dtype_bytes``；
    - ``arch_meta`` 缺失 → 启发式 ``W × 0.1 × (ctx/8192) × (dtype_bytes/2)``（精度档按字节数线性折算）。

    ``kv_layers`` 是**有 KV 的层数**：混合线性注意力模型（GDN/Mamba 层）只有部分层存 KV，
    按全层算会高估数倍，把本该放行的部署误拒。

    **这个数是「够不够装下 n_parallel 条满长请求」的容量估算，不是引擎启动时的实际占用**：
    引擎按 ``gpu_memory_utilization`` 把权重之外的显存整块切成 KV 池，池子大小与
    ``max_model_len`` 无关；启动校验只要求池子装得下一条满长。因此判定可部署性时
    ``n_parallel`` 取默认的 1（对齐引擎校验）；用户显式配 ``kv_cache.n_parallel`` 才按
    更高并发估峰值——那是容量口径，不是准入口径。

    MLA 类架构（DeepSeek 压缩 KV）不走真公式：它没有 ``num_kv_heads``×``head_dim`` 这个口径，
    落到上面的启发式是**偏高估**的一侧——只会让边界部署被误拒，比低估放行后 OOM 安全。
    """
    if not has_kv_cache or not context_size or weight_gb is None:
        return 0.0
    if arch_meta is not None:
        n_seq = int(n_parallel) if n_parallel else DEFAULT_N_PARALLEL
        raw = (
            2
            * n_seq
            * int(context_size)
            * (arch_meta.kv_layers or arch_meta.num_layers)
            * arch_meta.num_kv_heads
            * arch_meta.head_dim
            * float(dtype_bytes)
        )
        return round(raw / (1024**3), 2)
    heuristic = weight_gb * KV_BASE_RATIO * (context_size / KV_BASE_CONTEXT)
    return round(heuristic * (float(dtype_bytes) / DEFAULT_KV_DTYPE_BYTES), 2)


class OffloadTier(BaseModel):
    """逐层表的一行：前 N 层上 GPU 时的内存/显存需求。"""

    offload_layers: int = Field(..., description="上 GPU 的层数（0=纯 CPU）")
    ram_gb: float = Field(..., description="该档内存需求（未 offload 的权重 + KV(落内存时) + OS 保留）")
    vram_gb: float = Field(..., description="该档每卡显存需求（offload 的权重 + KV(落显存时)）")
    feasible: bool = Field(..., description="该档在当前两个池子下是否同时满足显存/内存预算")


class MemoryEstimate(BaseModel):
    """单节点（或单集群口径）的资源测算结果。"""

    weight_gb: float
    kv_gb: float = Field(..., description="KV cache 占用（GiB，按所选精度档折算）")
    total_layers: int
    tiers: list[OffloadTier] = Field(default_factory=list, description="offload_layers=0..total 逐档")
    scenario: str = Field(..., description="取解场景：gpu_only（全量上卡）| cpu_only（纯 CPU）")
    recommended_tier: int = Field(..., description="推荐 offload 层数（tiers 的下标即层数）")
    vram_avail_gb: float = 0.0
    ram_avail_gb: float = 0.0
    kv_source: str = Field("formula", description="KV 口径来源：formula（真公式）| heuristic（元数据缺失兜底）")


def _tier_at(weight_gb: float, kv_gb: float, total: int, n: int, parallel: int = 1) -> OffloadTier:
    """第 n 档的（内存, 每卡显存）需求。

    - ``n == 0``（纯 CPU）没有卡：KV 只能落内存（llama.cpp ``-ngl 0`` 时 KV 本来就在主机
      内存）——否则无卡节点会因「显存放不下 KV」被判无解；``n > 0`` 时 KV 恒随权重上卡
      （KV 落点不可配，见模块 docstring）；
    - ``parallel`` = 该份 tp×pp：显存侧按卡均摊（与 ``estimate_vram_gb`` 同口径），
      内存侧不分摊（未 offload 的权重整份在内存）。
    """
    ratio = n / total
    kv_on_gpu = n > 0
    vram = (ratio * weight_gb + (kv_gb if kv_on_gpu else 0.0)) / max(1, int(parallel))
    ram = (1 - ratio) * weight_gb + (0.0 if kv_on_gpu else kv_gb)
    return OffloadTier(offload_layers=n, ram_gb=round(ram, 2), vram_gb=round(vram, 2), feasible=False)


def _scenario_of(offload_layers: int) -> str:
    """档位场景名（0=纯 CPU、>0=纯 GPU）。

    取解只在两端（``allowed`` 只含 0 与 total_layers），故本函数实际只产出
    gpu_only / cpu_only；``gpu_main`` / ``cpu_main`` 是**偏好**取值（``offload_preference``
    可传），不是取解结果。
    """
    return SCENARIO_CPU_ONLY if offload_layers <= 0 else SCENARIO_GPU_ONLY


def is_cpu_only(memory: "MemoryEstimate | None") -> bool:
    """该测算结果是否为纯 CPU 部署（不占卡）。

    调用方据此跳过 GPU 语义的处理（推荐调度解释、显存分流提示、GPU 参数注入等）——
    部署形态由测算结果给出，调用侧不写引擎名或卡数假设。
    """
    return memory is not None and memory.scenario == SCENARIO_CPU_ONLY


def estimate_memory_requirements(
    *,
    weight_gb: float | None,
    kv_gb: float,
    total_layers: int | None,
    vram_avail_gb: float,
    ram_avail_gb: float,
    allow_cpu: bool = False,
    preference: str = PREF_AUTO,
    headroom: float = DEFAULT_HEADROOM,
    kv_source: str = "formula",
    parallel: int = 1,
) -> MemoryEstimate | None:
    """逐层表 + 三约束 + 场景求解；权重/层数未知 → None（调用方退回旧单池口径）。

    取解只在**两端**：全量上卡（``total_layers``）与纯 CPU（0）——中间混合档不自动取。
    理由：层数语义随 llama.cpp 版本变化（新版把 output 层算进 N），部分层上卡还会让
    「账本预留的显存 vs 引擎实际占用」脱钩；用户要调混合层数请自己加 ``--n-gpu-layers``
    （该 flag 不在 protected_flags 里，可覆盖系统值）。

    ``allow_cpu=False`` 只允许全量上卡一档（纯显存部署）；开启后两端都可取，由偏好决定
    优先哪端：``auto`` / ``gpu_main`` 优先全量上卡，``cpu_main`` / ``cpu_only`` 优先纯 CPU。
    显式 ``cpu_only`` 且纯 CPU 不可行、显式 ``gpu_only`` 且全量上卡不可行时都返回 None
    （不静默改用另一种部署）——两端偏好都是硬约束，避免部署形态静默漂移。
    """
    if weight_gb is None or weight_gb <= 0 or not total_layers or total_layers < 1:
        return None

    tiers = [_tier_at(weight_gb, kv_gb, total_layers, n, parallel) for n in range(total_layers + 1)]
    vram_cap = max(0.0, vram_avail_gb) * headroom
    ram_cap = max(0.0, ram_avail_gb) * headroom
    # 约束①：权重总量必须放得下（可拆，看两池之和）
    total_ok = weight_gb <= max(0.0, vram_avail_gb) + max(0.0, ram_avail_gb) + 1e-9
    for tier in tiers:
        tier.feasible = total_ok and tier.vram_gb <= vram_cap + 1e-9 and tier.ram_gb <= ram_cap + 1e-9

    feasible = [t.offload_layers for t in tiers if t.feasible]
    if not feasible:
        return None

    # 取解只在两端（全量上卡 / 纯 CPU）——中间混合档不自动取，理由见函数 docstring。
    # 显式 cpu_only 只在纯 CPU 可行时取解、gpu_only 只在全量上卡可行时取解（不静默换部署形态）。
    if preference == SCENARIO_CPU_ONLY:
        allowed = [0] if 0 in feasible else []
    elif preference == SCENARIO_GPU_ONLY:
        allowed = [total_layers] if total_layers in feasible else []
    elif allow_cpu:
        allowed = [n for n in feasible if n in (0, total_layers)]
    else:
        allowed = [total_layers] if total_layers in feasible else []
    if not allowed:
        return None

    chosen = _resolve_tier(allowed, total_layers, preference)
    return MemoryEstimate(
        weight_gb=round(weight_gb, 2),
        kv_gb=round(kv_gb, 2),
        total_layers=total_layers,
        tiers=tiers,
        scenario=_scenario_of(chosen),
        recommended_tier=chosen,
        vram_avail_gb=round(vram_avail_gb, 2),
        ram_avail_gb=round(ram_avail_gb, 2),
        kv_source=kv_source,
    )


def _resolve_tier(allowed: list[int], total: int, preference: str) -> int:
    """在可行档位里按偏好取点（``allowed`` 只含两端：0 与 total_layers）。

    - ``cpu_only`` / ``cpu_main``：优先纯 CPU（0），不可行才退全量上卡；
    - ``gpu_only`` / ``gpu_main`` / ``auto``：优先全量上卡，不可行才退纯 CPU。

    ``allowed`` 已由调用方按部署形态收窄（显式 cpu_only 时只含 0、gpu_only 只含 total），
    故此处只排序、不再判部署形态。
    """
    prefer_cpu = preference in CPU_PREFERENCES
    if prefer_cpu:
        return 0 if 0 in allowed else total
    return total if total in allowed else 0


def wants_cpu_lane(allow_cpu: bool, preference: str) -> bool:
    """请求是否放行纯 CPU 部署：``allow_cpu`` 显式放行，或显式 ``cpu_only`` 偏好隐含放行。

    部署语义的唯一定义处——API 准入、创建预检、编排求解、扩缩拒绝都读它。写散在多处
    就会漏改（曾经有一处只认 ``allow_cpu``，于是显式 ``cpu_only`` 的请求被 API 放行、
    编排却静默按显存档部署）。
    """
    return bool(allow_cpu) or preference == SCENARIO_CPU_ONLY


@dataclass(frozen=True)
class LaneCandidate:
    """一个候选节点对单个引擎实例的可承载能力（全量上卡档，取该节点单卡最大可用显存）。"""

    node_id: str
    vram_avail_gb: float
    ram_avail_gb: float


@dataclass(frozen=True)
class LanePlan:
    """部署形态决策：求解结果 + 推荐调度。

    场景与档位需求（层数/显存/内存）都从 ``memory`` 的**取解档**读，不再各存一份——
    存了就会与 ``memory`` 漂移（改一处忘一处即两份口径）。
    """

    node_id: str | None
    memory: MemoryEstimate

    @property
    def tier(self) -> OffloadTier:
        """取解档（``memory.tiers`` 里的推荐那一行）。"""
        return self.memory.tiers[self.memory.recommended_tier]

    @property
    def scenario(self) -> str:
        """取解场景（gpu_only / cpu_only）。"""
        return self.memory.scenario

    @property
    def offload_layers(self) -> int:
        """取解档的 offload 层数（0 = 纯 CPU）。"""
        return self.memory.recommended_tier

    @property
    def vram_gb(self) -> float:
        """该档每卡显存需求（GiB）。"""
        return self.tier.vram_gb

    @property
    def ram_gb(self) -> float:
        """该档内存需求（GiB）。"""
        return self.tier.ram_gb


def lane_candidates(nodes, gpus, ledger, *, gpu_archs=None, pinned: str | None = None) -> list[LaneCandidate]:
    """部署形态求解的候选池装配（**唯一入口**）。

    **两池都取账本可用量**（``ResourceLedger.get_available*`` 内部已按利用率上限折过
    10% 余量），故下游不再折——「折几次」只在本函数决定，调用方无从折错。此前
    「显存取账本（已折）+ 内存取节点实时原值（未折）」混进同一次求解，让内存侧白丢
    10% 余量，就是这类混源的典型。

    显存取该节点上**引擎可用架构**卡的单卡最大可用值（一个引擎实例落在一张卡上，
    跨卡不累加；未列 ``gpu_archs`` 则不过滤架构）；内存取节点级账本可用量（含别家
    已预留的扣减）。``pinned`` 只保留指定节点——不在列表里就返回空池，怎么报由调用方定。

    显存侧在账本可用之上再叠**心跳实况**闸（取两者较小值）：卡被外部进程占着时账本
    看不见（它只记平台自己的预留），只看账本会解出 GPU 形态、随后调度的物理闸
    （``physical_fits``）逐卡判「显存不足」直接失败——求解器与调度闸必须看同一个
    世界。心跳缺失（节点未上报）时维持纯账本口径（乐观，与物理闸的跳过语义一致）；
    物理可用 = total − 心跳已用，不再乘利用率上限（used 是实况占用，与账本折让
    叠乘会双重折让）。
    """
    if pinned:
        nodes = [n for n in nodes if n.node_id == pinned]
    archs = set(gpu_archs) if gpu_archs else None
    vram_by_node: dict[str, float] = {}
    for g in gpus:
        if archs is not None and getattr(g, "arch", None) not in archs:
            continue
        avail = ledger.get_available(g.uuid, g.total_vram_gb)
        # 物理闸同式（total − 心跳已用，不乘利用率上限——used 是实况，叠乘会双重折让）
        used = getattr(g, "used_vram_gb", None)
        if used is not None:
            avail = min(avail, max(0.0, g.total_vram_gb - used))
        vram_by_node[g.node_id] = max(vram_by_node.get(g.node_id, 0.0), avail)
    return [
        LaneCandidate(
            node_id=n.node_id,
            vram_avail_gb=vram_by_node.get(n.node_id, 0.0),
            ram_avail_gb=ledger.get_available_ram(n.node_id, n.memory_gb),
        )
        for n in nodes
    ]


def lane_estimates(
    candidates: list[LaneCandidate],
    *,
    weight_gb: float | None,
    kv_gb: float,
    total_layers: int | None,
    allow_cpu: bool = False,
    preference: str = PREF_AUTO,
    kv_source: str = "formula",
    parallel: int = 1,
) -> list[tuple[LaneCandidate, MemoryEstimate]]:
    """逐候选求解并保留**全部**节点结果（不止最优）。

    逐层表是「该节点能不能放」的事实，调用方常要拿它展示逐节点结论（inspect 的
    node_support）——只回最优会逼调用方自己重算一遍，口径随即分叉。``candidates`` 须来自
    :func:`lane_candidates`（已折过），故此处按池原值判，不再折 headroom。
    """
    out: list[tuple[LaneCandidate, MemoryEstimate]] = []
    for cand in candidates:
        est = estimate_memory_requirements(
            weight_gb=weight_gb,
            kv_gb=kv_gb,
            total_layers=total_layers,
            vram_avail_gb=cand.vram_avail_gb,
            ram_avail_gb=cand.ram_avail_gb,
            allow_cpu=allow_cpu,
            preference=preference,
            headroom=1.0,
            kv_source=kv_source,
            parallel=parallel,
        )
        if est is not None:
            out.append((cand, est))
    return out


def plan_lane(
    *,
    weight_gb: float | None,
    kv_gb: float,
    total_layers: int | None,
    candidates: list[LaneCandidate],
    allow_cpu: bool = False,
    preference: str = PREF_AUTO,
    kv_source: str = "formula",
    parallel: int = 1,
) -> LanePlan | None:
    """逐候选节点求解部署形态，取场景最优、同场景余量最大的调度结果（无解 → None）。

    场景序即优先级（显存满上最优）；同场景优先选可用池更大的节点，把碎片留给后续。
    ``candidates`` 必须来自 :func:`lane_candidates`（两池已按利用率上限折过），故内部
    不再折 headroom——见 :func:`lane_estimates`。需要逐节点结论的调用方直接调
    :func:`lane_estimates` 再 :func:`best_from_estimates`，不必自己重算一遍表。
    """
    return best_from_estimates(
        lane_estimates(
            candidates,
            weight_gb=weight_gb,
            kv_gb=kv_gb,
            total_layers=total_layers,
            allow_cpu=allow_cpu,
            preference=preference,
            kv_source=kv_source,
            parallel=parallel,
        )
    )


def best_from_estimates(estimates: list[tuple[LaneCandidate, "MemoryEstimate"]]) -> LanePlan | None:
    """从 :func:`lane_estimates` 的逐节点结果里取最优部署形态（场景优先，同场景池大者优先）。"""
    best: LanePlan | None = None
    best_key: tuple[int, float] | None = None
    for cand, est in estimates:
        key = (SCENARIOS.index(est.scenario), -(cand.ram_avail_gb + cand.vram_avail_gb))
        if best_key is None or key < best_key:
            best_key = key
            best = LanePlan(node_id=cand.node_id, memory=est)
    return best


def forced_cpu_lane(
    *,
    weight_gb: float | None,
    kv_gb: float,
    total_layers: int | None,
    candidates: list[LaneCandidate],
    kv_source: str = "formula",
) -> LanePlan:
    """强制部署的纯 CPU 兜底档：正常求解无解时仍给一个调度结果（超额记账）。

    :func:`plan_lane` 无解即拒——显式要纯 CPU 却静默改用显存会让部署形态漂移。用户显式强制
    部署（``force=true``）时改走这里：需求照逐层表算，调度位置取可用内存最大的候选节点，
    容量校验交引擎/OOM 兜底，与显存侧 ``force_reserve`` 同口径。
    """
    if not candidates:
        raise ValueError("forced_cpu_lane 需要至少一个候选节点")
    best = max(candidates, key=lambda c: (c.ram_avail_gb, c.node_id))
    total = max(1, int(total_layers or 1))
    tier = _tier_at(float(weight_gb or 0.0), kv_gb, total, 0)
    return LanePlan(
        node_id=best.node_id,
        memory=MemoryEstimate(
            weight_gb=round(float(weight_gb or 0.0), 2),
            kv_gb=round(kv_gb, 2),
            total_layers=total,
            tiers=[tier],
            scenario=SCENARIO_CPU_ONLY,
            recommended_tier=0,
            vram_avail_gb=round(best.vram_avail_gb, 2),
            ram_avail_gb=round(best.ram_avail_gb, 2),
            kv_source=kv_source,
        ),
    )


# 全量上卡的版本无关写法：llama.cpp 各版本都把「大数」钳到全部层，而精确层数语义跨版本
# 不一致（新版把 output 层算进 N，老版不算）——所以「全量上卡」一律发 999，不发明细层数。
# 系统只在两端取解（本值 / 0），中间层数由用户在部署参数里用 ``--n-gpu-layers`` 自行覆盖。
NGL_ALL = 999
