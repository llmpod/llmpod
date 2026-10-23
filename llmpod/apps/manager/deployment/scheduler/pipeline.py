"""逐引擎选卡管线：Filter 链 → 候选生成器阶梯 → Scorer 链。

外层 ``gpu.plan_placement`` fold 保留部署级联合约束（跨引擎卡排重 used_uuids、多机
分片节点互异 exclude_nodes、VRAM 预留原子性——先整体规划后统一预留）；本模块只回答
「一个引擎实例在给定约束下选哪组卡」，三级各自独立可测、可扩展（加规则 = 加链节点）：

- **Filter 链**：逐卡否决，谁否决谁署名（``rejections`` 计数聚合，inspect「为什么
  这张卡灰了」的数据源）。固定序：online → arch → 容量（card_ok 注入）。
- **候选生成器阶梯**：单节点 → 跨机组合（等卡数节点组），有序生成、首个非空即用
  （级别即优先级）。节点内取卡时做显存档统一——同引擎的卡须同显存档（跨档 TP 会被小卡拖死）。
- **Scorer 链**：求和裁决。调度策略 binpack（默认，装得下里挑最紧——省出整节点）/
  spread（挑最宽裕）+ 模型文件本地性（已有模型文件的节点强加分——省一次完整下发）
  + 卡痕迹偏好（无部署痕迹的卡优先；有停止部署残留的次之；仍被占用的最次）。

优先级约定（全仓统一）：**用户显式指定永远赢 → 未指定才推导 → 推导有确定兜底序**。
手选卡（wanted_uuids）是显式指定：管线收窄为纯校验（分布/显存档/容量），阶梯与
scorer 跳过。全部候选确定性排序（node_id / index / uuid），同分不随机。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Callable, Mapping

from llmpod.apps.manager.deployment.constants import CardState
from llmpod.config.constants import GpuStatus
from llmpod.core.exceptions import LLMPodValidationError

log = logging.getLogger("llmpod.apps.manager.deployment.scheduler.pipeline")

# 本地性加分须压过策略分差（binpack/spread 的分差是个位数量级），但不影响可行性——
# 可行性由 Filter/阶梯负责，scorer 只在可行候选内裁决
LOCALITY_BONUS = 1000.0
# 卡痕迹降权（软偏好，不否决）：无部署痕迹的卡最优，有已停止/失败部署残留的卡次之，
# 仍被别的运行中部署占用的卡最次。量级夹在中间——小于 LOCALITY_BONUS（节点已有模型
# 文件省掉的是整包下发），大于策略分差（否则等于没写）。
CARD_RESIDUAL_PENALTY = 100.0
CARD_SHARED_PENALTY = 200.0
# 降权上限：卡级罚分逐张求和，大节点（如 8 卡全 shared）会累到超过 LOCALITY_BONUS、
# 把「本地已有模型文件」这个更值钱的偏好反压掉。封顶保证排序语义恒定「本地性 > 卡痕迹」。
# 粒度是**整份调度**而不是单节点——跨机调度时按节点封顶挡不住多节点累加（3 节点各 400
# 就凑出 1200），封顶就白设了。
CARD_STATE_PENALTY_CAP = 500.0

# 事实值（CardState）→ 罚分：查表用模块级常量，别在每次调用里现造 dict（卡级循环里会调用
# 逐卡 × 逐候选次）
_CARD_STATE_PENALTY: dict[str, float] = {
    CardState.RESIDUAL: CARD_RESIDUAL_PENALTY,
    CardState.SHARED: CARD_SHARED_PENALTY,
}
# 跨机组合枚举的节点池上限：合格节点多于此时取 node_id 最小的前 K 个再组合，防组合爆炸
_MAX_COMBO_POOL = 8
# 显存档取整精度（GB）：同档 = round(total, 1) 相等
_TIER_PRECISION = 1


# ==================== ① Filter 链 ====================


@dataclass(frozen=True)
class CardFilter:
    """逐卡过滤器：``check(gpu)`` 返回 None=通过 / 短原因=否决（署名进 rejections）。

    原因串**直接是给用户看的说法**（如「节点离线」「显存不足」）——它会原样进部署失败的
    ``last_error`` 与手选卡否决说明，所以别写成内部记号（曾经写过 ``offline``，再靠一张
    名→说法的映射表翻译，两处都要维护）。
    """

    name: str
    check: Callable[[Any], str | None]


def _f_online(g) -> str | None:
    return None if getattr(g, "status", GpuStatus.ONLINE) == GpuStatus.ONLINE else "节点离线"


def _f_arch(arch: str) -> Callable[[Any], str | None]:
    def check(g) -> str | None:
        return None if getattr(g, "arch", "") == arch else f"GPU 架构不匹配（arch≠{arch}）"

    return check


def _f_capacity(card_ok: Callable[[Any], bool]) -> Callable[[Any], str | None]:
    def check(g) -> str | None:
        return None if card_ok(g) else "显存不足"

    return check


def default_filters(
    arch: str | None,
    card_ok: Callable[[Any], bool] | None = None,
) -> list[CardFilter]:
    """固定序 Filter 链：online → arch → 容量。加规则 = 往后追加链节点（顺序即语义）。

    arch=None 跳过架构过滤——手选卡场景架构约束由选集配对校验承担（份内同节点
    同架构），不按部署单 arch 否决，异构多副本（nvidia 份 + cann 份混选）照常放行。
    """
    filters = [CardFilter("online", _f_online)]
    if arch is not None:
        filters.append(CardFilter("arch", _f_arch(arch)))
    if card_ok is not None:
        filters.append(CardFilter("capacity", _f_capacity(card_ok)))
    return filters


def run_filters(gpu_pool, filters: list[CardFilter]) -> tuple[dict[str, list], dict[str, int], dict[str, str]]:
    """逐卡跑 Filter 链；返回 (node_id → 通过卡列表(按 index 排序), filter名 → 否决数, filter名 → 首个否决原因)。

    原因串一并带出来：此前只留计数，部署失败时用户看到的只有「某某×N」，得靠猜是离线、
    架构不符还是显存不够。``reasons`` 取每个 filter 的首个原因即可——同一 filter 对所有卡
    给出的原因形状一致（如 ``arch≠cann``），样本量由计数表达。
    """
    passed: dict[str, list] = defaultdict(list)
    rejections: dict[str, int] = defaultdict(int)
    reasons: dict[str, str] = {}
    for g in gpu_pool:
        for f in filters:
            reason = f.check(g)
            if reason is not None:
                rejections[f.name] += 1
                reasons.setdefault(f.name, str(reason))
                break
        else:
            passed[getattr(g, "node_id", "")].append(g)
    for cards in passed.values():
        cards.sort(key=lambda g: (getattr(g, "index", 0), getattr(g, "uuid", "")))
    return passed, dict(rejections), reasons


# ==================== 引擎请求 / 调度结果 ====================


@dataclass(frozen=True)
class EngineRequest:
    """单引擎实例的选卡请求（fold 内逐引擎构造，携带部署级联合约束的当量）。"""

    engine_id: str
    need_per_node: int  # tp×pp×ep ÷ nnodes
    nnodes: int  # 引擎跨机节点数（1=单节点）
    arch: str
    used_uuids: frozenset[str] = frozenset()  # 本部署已占卡（跨引擎排重）
    exclude_nodes: frozenset[str] = frozenset()  # 本引擎已落节点（多机分片互异）
    target_node: str | None = None  # 指定单节点（显式指定）
    target_nodes: frozenset[str] | None = None  # 选节点集（显式指定）
    wanted_uuids: tuple[str, ...] | None = None  # 手选卡（显式指定，最高优先级）
    locality_node_ids: frozenset[str] = frozenset()  # 已有模型文件的节点（本地性加分）
    # 卡的部署痕迹（uuid → "residual"（有已停止/失败部署残留）/ "shared"（仍被别的
    # 运行中部署占用））；只影响可行候选间的裁决顺序，不做否决。事实由 service 层出，
    # 权重由本模块常量定。
    card_state: Mapping[str, str] = field(default_factory=dict)
    policy: str = "binpack"  # binpack | spread
    # 自动路径的 arch 候选序：None = 未给候选序（只用 arch）；非空 = 逐候选试探，前面的
    # 放不下才落后面的（异构副本，份内同 arch 由单 arch Filter 保证）；**空元组 = 候选序
    # 为空**（上游把架构全闸掉了，如本拓扑服务不了或没有匹配镜像）→ 直接否决。空必须与
    # None 区分：当成 None 处理会静默回退到未经闸门的 arch，把闸门变成不生效。
    arch_order: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Placement:
    """选卡结果：逐分片 (node_id, cards)；有序（head 分片在前）。"""

    shards: tuple[tuple[str, list], ...]
    ladder: str  # 命中的候选生成器级别（级别即优先级）


class PipelineRejection(LLMPodValidationError):
    """全阶梯失败：聚合各 Filter 署名计数 + 命中的阶梯级别，给人读的原因。

    ``rejections``（filter 名 → 否决卡数）是 inspect「为什么这张卡灰了」的数据源；
    异常本体仍是 422 语义（LLMPodValidationError）。
    """

    def __init__(
        self,
        message: str,
        *,
        rejections: dict[str, int] | None = None,
        ladder: str = "",
        user_reason: str | None = None,
        reasons: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.rejections = rejections or {}
        self.ladder = ladder
        # 各 filter 的首个否决原因（filter 名 → 人读句）。探针/回显消费方据此把计数
        # 渲染成人话——不带上它，每个消费方都会自造一张「代号→说法」映射表，两处维护
        self.reasons = reasons or {}
        # 给用户看的中文原因（部署失败时落到 deployment.last_error）。``message`` 保留
        # 原文供排障——它带 ladder 与计数明细，说给用户听太内部。
        self.user_reason = user_reason


# ==================== ② 候选生成器阶梯 ====================


def _tier_uniform(cards: list, need: int) -> list | None:
    """从节点卡列表取同显存档的 need 张（档位取首张所属档；不够则该节点不合格）。"""
    by_tier: dict[float, list] = defaultdict(list)
    for g in cards:
        by_tier[round(float(getattr(g, "total_vram_gb", 0) or 0), _TIER_PRECISION)].append(g)
    for t in sorted(by_tier, reverse=True):
        if len(by_tier[t]) >= need:
            return by_tier[t][:need]
    return None


def _eligible_picks(scoped: dict[str, list], req: EngineRequest) -> tuple[dict[str, list], dict[str, int]]:
    """逐节点挑选：排除集/已占卡过滤 + 同显存档取 need 张。

    返回 (合格节点 → 卡组, 各节点剩余可用卡数)。单节点与跨机两级候选共用——
    节点合格性与容量口径只有一份。
    """
    eligible: dict[str, list] = {}
    capacity: dict[str, int] = {}
    for nid, cards in sorted(scoped.items()):
        if nid in req.exclude_nodes:
            continue
        avail = [g for g in cards if getattr(g, "uuid", "") not in req.used_uuids]
        capacity[nid] = len(avail)
        # 节点内取卡顺序：按痕迹降权升序（无痕迹 → residual → shared），有痕迹的只在同档
        # 干净卡不够时补位。不排序的话同档取哪张卡取决于池序，卡偏好就只在节点粒度生效。
        # 稳定排序即可——run_filters 已把节点内卡按 (index, uuid) 排好，同罚分保持原序。
        if req.card_state:
            avail = sorted(avail, key=lambda g: card_state_penalty(req.card_state.get(getattr(g, "uuid", ""))))
        picked = _tier_uniform(avail, req.need_per_node)
        if picked is not None:
            eligible[nid] = picked
    return eligible, capacity


def _single_node_candidates(scoped: dict[str, list], req: EngineRequest) -> tuple[list[Placement], dict[str, int]]:
    """级别一（单节点引擎）：每合格节点一个候选（排除集生效）。附带各节点剩余容量。"""
    eligible, capacity = _eligible_picks(scoped, req)
    out = [Placement(shards=((nid, picked),), ladder="single_node") for nid, picked in eligible.items()]
    return out, capacity


def _cross_node_candidates(scoped: dict[str, list], req: EngineRequest) -> tuple[list[Placement], dict[str, int]]:
    """级别二（跨机引擎）：等卡数节点组——nnodes 个互异节点、各出 need 张同档卡。"""
    eligible, capacity = _eligible_picks(scoped, req)
    if len(eligible) < req.nnodes:
        return [], capacity
    # 截断下限抬到 nnodes：池上限低于要求的节点数时 combinations 恒空，跨机部署会
    # 永远排不开（报「在线卡不足」而集群其实放得下）
    pool = sorted(eligible)[: max(_MAX_COMBO_POOL, req.nnodes)]
    out: list[Placement] = [
        Placement(shards=tuple((nid, eligible[nid]) for nid in combo), ladder="cross_node")
        for combo in combinations(pool, req.nnodes)
    ]
    return out, capacity


# ==================== ③ Scorer 链 ====================


def _policy_scorer(req: EngineRequest, node_capacity: dict[str, int]):
    """调度策略（组级裁决：组合内各节点剩余容量**之和**）。

    - binpack：取负和 → 选剩余总量最小的节点组——用小容量节点凑齐，把大节点的
      完整容量留给后续更宽的引擎，避免余量被打成碎片；
    - spread：取正和 → 选剩余总量最大的节点组，尽早摊开。
    ``node_capacity`` = 该节点过滤后剩余 eligible 卡数（不含本次选走的），由候选
    生成器带回——scorer 只读不改。
    """

    def combo_score(nid: str, cards: list) -> float:
        avail = float(node_capacity.get(nid, len(cards)))
        return -avail if req.policy == "binpack" else avail

    return combo_score


def _locality_scorer(req: EngineRequest):
    def node_score(nid: str, cards: list) -> float:
        return LOCALITY_BONUS if nid in req.locality_node_ids else 0.0

    return node_score


def card_state_penalty(state: str | None) -> float:
    """卡痕迹的降权值（0 = 无痕迹最优）。权重常量只在本模块定义，事实值来自 CardState。"""
    return _CARD_STATE_PENALTY.get(state or "", 0.0)


def trace_penalty_scorer(req: EngineRequest) -> Callable[[str, list], float]:
    """卡痕迹的降权量（**正数**，越大越差）：按节点把卡级罚分求和。

    无痕迹的卡不出现于 ``card_state``（得 0，最优）；有已停止/失败部署残留的卡扣
    ``CARD_RESIDUAL_PENALTY``；仍被别的运行中部署占用的卡扣更多。只影响裁决顺序——
    干净卡不可行（显存/架构/数量）时照旧用有痕迹的卡。

    与 :func:`default_scorers` 配对使用：这里只给**未封顶**的原始量，封顶由调用方在
    **整份调度**上做（``CARD_STATE_PENALTY_CAP``）——按节点封顶挡不住多节点累加。
    """

    def node_penalty(nid: str, cards: list) -> float:
        return sum(card_state_penalty(req.card_state.get(getattr(g, "uuid", ""))) for g in cards)

    return node_penalty


def default_scorers(req: EngineRequest, node_capacity: dict[str, int]) -> list[Callable[[str, list], float]]:
    """策略 Scorer 链（求和裁决）：调度策略 + 模型文件本地性。加规则 = 追加链节点。

    ``req`` 的卡痕迹不在链里——它要按整份调度封顶，走 :func:`trace_penalty_scorer` 单独算。
    """
    return [_policy_scorer(req, node_capacity), _locality_scorer(req)]


def score_placement(
    placement: "Placement",
    *,
    scorers: list[Callable[[str, list], float]],
    trace_penalty: Callable[[str, list], float],
) -> float:
    """调度总分：策略链逐分片求和 − 卡痕迹降权，降权部分**按整份调度**封顶。

    「整份调度」是关键：跨机调度把各节点扣分相加，按节点封顶挡不住（3 节点各 400 就凑出
    1200 > ``LOCALITY_BONUS``），「本地性 > 卡痕迹」的排序语义就没了。
    """
    policy = sum(s(nid, cards) for nid, cards in placement.shards for s in scorers)
    traced = sum(trace_penalty(nid, cards) for nid, cards in placement.shards)
    return policy - min(traced, CARD_STATE_PENALTY_CAP)


# ==================== 手选卡（显式优先：管线收窄为校验） ====================


def _manual_placement(scoped: dict[str, list], req: EngineRequest, vetoes: dict[str, str]) -> Placement:
    """手选卡：只对圈定卡集做分布/容量校验并调度，不走阶梯与 scorer（用户显式指定永远赢）。

    校验规则：wanted 扣除已占后，跨机引擎须分布在恰好 nnodes 个（未排除的）节点、
    每节点 need 张同档卡；单机引擎（nnodes==1）允许多节点桶——多副本手选时本引擎
    从剩余卡的最大桶取 need 张（每份落一个节点，桶选取与 ``pair_manual_replicas``
    同款贪心，确定性按桶大小降序 + 节点字典序）。指定卡被 Filter 链否决（离线/
    架构/显存）时 ``vetoes`` 携带署名原因——用户点名要这几张，静默换卡或含糊报错
    都更糟。本函数只消费本引擎的份额，wanted 是否被全部引擎覆盖由 plan_placement
    收口校验（多副本时单引擎必然剩其它份的卡）。
    """
    wanted = set(req.wanted_uuids or ()) - set(req.used_uuids)
    if vetoes:
        detail = "；".join(f"{u}: {r}" for u, r in sorted(vetoes.items()))
        raise LLMPodValidationError(f"指定 GPU 不可用（{detail}）")
    # 按 (节点, 架构) 分桶：同桶=份内同节点同架构（与 pair_manual_replicas 同款语义）
    by_bucket: dict[tuple[str, str], list] = defaultdict(list)
    for nid, cards in scoped.items():
        for g in cards:
            if getattr(g, "uuid", "") in wanted:
                by_bucket[(nid, getattr(g, "arch", ""))].append(g)
    shards: list[tuple[str, list]] = []
    if req.nnodes <= 1:
        # 单机引擎：wanted 可能分布在多个桶（多副本手选，每引擎一份）——取最大桶
        eligible = [(nid, cards) for (nid, _arch), cards in by_bucket.items() if len(cards) >= req.need_per_node]
        if not eligible:
            raise LLMPodValidationError(
                f"指定 GPU 无法满足节点分布：没有任何节点有 ≥{req.need_per_node} 张指定卡"
                f"（多副本手选每份需同节点同架构 {req.need_per_node} 张）"
            )
        eligible.sort(key=lambda kv: (-len(kv[1]), kv[0]))
        nid, on_node = eligible[0]
        picked = _tier_uniform(on_node, req.need_per_node)
        if picked is None:
            raise LLMPodValidationError(f"指定 GPU 在节点 {nid} 凑不出同显存档的 {req.need_per_node} 张")
        return Placement(shards=((nid, picked),), ladder="manual")

    # 跨机引擎：单引擎的卡不能跨架构 TP
    bucket_archs = {arch for (_nid, arch) in by_bucket}
    if len(bucket_archs) > 1:
        raise LLMPodValidationError(f"跨机手选须同架构（当前含 {'/'.join(sorted(bucket_archs))}）")
    by_node: dict[str, list] = defaultdict(list)
    for (nid, _arch), cards in by_bucket.items():
        by_node[nid].extend(cards)
    if len(by_node) != req.nnodes:
        raise LLMPodValidationError(
            f"指定 GPU 无法满足节点分布：需 {req.nnodes} 个节点各 {req.need_per_node} 张"
            f"（排除已占节点后实际落在 {len(by_node)} 个节点）"
        )
    for nid in sorted(by_node):
        on_node = sorted(by_node[nid], key=lambda g: (getattr(g, "index", 0), getattr(g, "uuid", "")))
        if len(on_node) < req.need_per_node:
            raise LLMPodValidationError(f"指定 GPU 在节点 {nid} 仅 {len(on_node)} 张，需 {req.need_per_node} 张")
        picked = _tier_uniform(on_node, req.need_per_node)
        if picked is None:
            raise LLMPodValidationError(f"指定 GPU 在节点 {nid} 凑不出同显存档的 {req.need_per_node} 张")
        shards.append((nid, picked))
    return Placement(shards=tuple(shards), ladder="manual")


# ==================== 主入口 ====================


# 空回落序（架构闸门淘汰了全部候选）的用户说法。调度内核抛的异常与 inspect 侧的
# ``gpu._probe_reason`` 共用同一句——同一个失败两条路径两种口径，用户就得学两套话。
NO_USABLE_ARCH_REASON = (
    "当前引擎/拓扑在集群里没有可用的架构（如 Ascend 不支持 pp>1、或该镜像 tag 无匹配条目）；"
    "请调整并行度、改选镜像或换集群"
)


def place_engine(
    gpu_pool,
    req: EngineRequest,
    *,
    card_ok: Callable[[Any], bool] | None = None,
) -> Placement:
    """跑完整管线为一个引擎选卡；全阶梯失败抛 :class:`PipelineRejection`（署名聚合）。

    手选卡（wanted_uuids）：不按部署 arch 否决（异构多副本份间可混 arch，份内同
    架构由配对保证）。自动路径：按 ``arch``（未给候选序）或 ``arch_order`` 逐候选
    试探——前面的 arch 放不下才落后面的（异构副本：新份可落不同 arch，份内同
    arch 仍由单 arch Filter 保证）；``arch_order`` 为空元组 = 上游架构闸门淘汰了
    全部候选，直接否决（不回退到未经闸门的 ``arch``）。
    """
    # 手选卡不按部署单 arch 否决（异构多副本份间可混 arch，份内同架构由配对保证）
    if req.wanted_uuids is not None:
        return _place_once(gpu_pool, req, default_filters(None, card_ok), req.arch)

    if req.arch_order is not None and not req.arch_order:
        raise PipelineRejection(
            f"no usable arch for engine {req.engine_id}（上游架构闸门淘汰了全部候选："
            f"本拓扑服务不了的架构 / 没有匹配镜像）",
            rejections={},
            ladder="arch_gate",
            user_reason=NO_USABLE_ARCH_REASON,
        )
    candidates = req.arch_order if req.arch_order is not None else (req.arch,)
    first_err: PipelineRejection | None = None
    for arch_cand in candidates:
        try:
            return _place_once(gpu_pool, req, default_filters(arch_cand, card_ok), arch_cand)
        except PipelineRejection as e:
            first_err = first_err or e
    assert first_err is not None
    raise first_err


def _place_once(gpu_pool, req: EngineRequest, filters: list[CardFilter], arch_label: str) -> Placement:
    """单 arch 口径跑一遍 Filter → 候选阶梯 → Scorer 裁决（place_engine 的单次尝试）。"""
    passed, rejections, reasons = run_filters(gpu_pool, filters)

    # 节点范围收窄：多机互异排除 + 指定节点/选节点集（显式范围）
    def _node_ok(nid: str) -> bool:
        if req.target_node and nid != req.target_node:
            return False
        if req.target_nodes and nid not in req.target_nodes:
            return False
        return True

    scoped = {nid: cards for nid, cards in passed.items() if _node_ok(nid)}

    if req.wanted_uuids is not None:
        # 手选卡的逐卡否决署名（wanted 中被 Filter 链拦下的卡 → 原因）
        wanted = set(req.wanted_uuids) - set(req.used_uuids)
        passed_uuids = {getattr(g, "uuid", "") for cards in passed.values() for g in cards}
        vetoes: dict[str, str] = {}
        for g in gpu_pool:
            guuid = getattr(g, "uuid", "")
            if guuid not in wanted or guuid in passed_uuids:
                continue
            for f in filters:
                reason = f.check(g)
                if reason is not None:
                    vetoes[guuid] = reason
                    break
        return _manual_placement(scoped, req, vetoes)

    if req.nnodes <= 1:
        candidates, capacity = _single_node_candidates(scoped, req)
        ladder = "single_node"
    else:
        candidates, capacity = _cross_node_candidates(scoped, req)
        ladder = "cross_node"
    if not candidates:
        raise PipelineRejection(
            _reject_message(req, rejections, ladder, arch_label),
            rejections=rejections,
            ladder=ladder,
            user_reason=_user_reject_reason(req, rejections, reasons, arch_label),
            reasons=reasons,
        )

    # 裁决：策略链求和 + 卡痕迹降权（同分按节点字典序最小，确定性）。链只建一次——
    # 候选可达组合数量级（封顶与合计口径见 score_placement）
    scorers = default_scorers(req, capacity)
    trace_penalty = trace_penalty_scorer(req)

    def _score(p: Placement) -> float:
        return score_placement(p, scorers=scorers, trace_penalty=trace_penalty)

    best = min(candidates, key=lambda p: (-_score(p), tuple(nid for nid, _ in p.shards)))
    return best


def _user_reject_reason(
    req: EngineRequest, rejections: dict[str, int], reasons: dict[str, str], arch_label: str
) -> str:
    """给用户看的中文失败原因（与 inspect 的 issues 同一套说法）。

    调度期的原始 message 是英文内部句式 + ladder 代号 + 计数明细，说给用户听等于没说；
    这里改用 filter 自己返回的说法（见 ``CardFilter``），并带上各自拦下了几张卡。
    """
    scope = "同一节点" if req.nnodes <= 1 else f"{req.nnodes} 个不同节点"
    head = f"没有能放下该引擎的 {arch_label} 卡组合（需要{scope}凑齐 {req.need_per_node} 张）"
    hits = [(name, count) for name, count in sorted(rejections.items()) if count]
    if not hits:
        return f"{head}：卡池里没有符合架构的在线卡"
    parts = [f"{reasons.get(name, name)} ×{count}" for name, count in hits]
    return f"{head}：{'；'.join(parts)}"


def _reject_message(req: EngineRequest, rejections: dict[str, int], ladder: str, arch_label: str) -> str:
    parts = [f"{name}×{n}" for name, n in sorted(rejections.items())]
    detail = f"；过滤明细 {', '.join(parts)}" if parts else ""
    if req.nnodes <= 1:
        return (
            f"no node with {req.need_per_node} available {arch_label} GPU(s) "
            f"for engine {req.engine_id}（ladder={ladder}{detail}）"
        )
    return (
        f"no {req.nnodes} distinct nodes with {req.need_per_node} available "
        f"{arch_label} GPU(s) for engine {req.engine_id}（ladder={ladder}{detail}）"
    )
