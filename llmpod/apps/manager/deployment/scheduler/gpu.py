"""GpuScheduler：GPU 分配 + VRAM 全局校验（有状态，依赖 ResourceLedger）。

单节点引擎（nnodes==1）：在同一节点分配 tp 张同架构同显存档 GPU。
跨机引擎（nnodes>1）：每节点分配 tp×pp÷nnodes 张，同副本分片落在 nnodes 个**不同**
节点；不同副本可共享节点。选卡内核在 ``pipeline.py``（Filter 链 → 候选阶梯 →
Scorer 链），本文件持有状态（ResourceLedger）与无状态入口（``plan_placement`` /
``recommend_placement`` / inspect 的结构性容量探测 ``probe_placement``）。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import NamedTuple

from llmpod.apps.manager.deployment.schemas import (
    GpuAssignment,
    GpuCandidate,
    GpuSelector,
    InstanceSpec,
    PlacementPrefs,
)
from llmpod.apps.manager.deployment.scheduler.pipeline import (
    NO_USABLE_ARCH_REASON,
    EngineRequest,
    place_engine,
)
from llmpod.config.constants import GpuStatus
from llmpod.apps.manager.deployment.resource_ledger import (
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_RAM_UTILIZATION,
    ResourceLedger,
)
from llmpod.core.exceptions import LLMPodValidationError

log = logging.getLogger("llmpod.apps.manager.deployment.scheduler.gpu")


def _cards_per_copy_sh(tp: int | None, pp: int | None) -> int:
    """一份副本占的卡数（tp×pp）。手选配对校验与大份优先调度排序共用的量纲。"""
    return max(1, int(tp or 1)) * max(1, int(pp or 1))


def infer_selection_arch(gpu_pool, gpu_uuids, default_arch: str) -> str:
    """手选卡 arch 推断：所选卡在池内 arch 唯一 → 取之；否则回退 default_arch。

    选 cann 卡 → cann 部署（解锁 Ascend）。``gpu_pool`` 为任意带 ``uuid / arch`` 属性的
    对象列表；inspect 校验与 orchestrator _plan 共用本口径，避免两处手写漂移。
    """
    if not gpu_uuids:
        return default_arch
    sel_archs = {g.arch for g in gpu_pool if g.uuid in gpu_uuids and g.arch}
    if len(sel_archs) == 1:
        return next(iter(sel_archs))
    return default_arch


def _validate_manual_selection(
    gpu_pool, gpu_uuids, *, tp: int, nnodes: int, arch: str, pp: int = 1, copies: int = 1
) -> None:
    """校验手动选卡集（结构 + 在线 + 节点分布/分组）；VRAM 留给 scheduler.reserve。

    ``gpu_pool`` 为任意带 ``uuid / node_id / arch / status`` 属性的对象列表
    （GpuCandidate 或 inspect 的 ClusterGpuInfo 均可）。规则：
    - 无重复、总数 == tp×pp×copies（每份 tp×pp 张；copies>1 时按份分组）；
    - 全部在线；
    - 单副本：全部卡同 arch（单引擎不能跨架构 TP）；
    - 单机份（nnodes==1）多副本：份间可异构——按 (node, arch) 桶贪心配份
      （桶内卡数须能凑出整数份），配不齐给逐桶余数的人读原因；
    - 跨机份（nnodes>1）：仅单副本放行（分布校验同旧规则）；多副本×跨机×手选组合
      的配对空间爆炸，v1 不做——多机多副本走自动调度。

    异构副本（各份形状不同）走 :func:`_validate_manual_selection_shapes`。
    """
    if not gpu_uuids:
        return
    parallel = tp * pp
    total_needed = parallel * copies
    if len(gpu_uuids) != len(set(gpu_uuids)):
        raise LLMPodValidationError("gpu_uuids 含重复 UUID")
    if len(gpu_uuids) != total_needed:
        word = "张（tp×pp）" if copies == 1 else f"张（每份 {parallel} 张 × {copies} 份）"
        raise LLMPodValidationError(f"手动选卡需 {total_needed} {word}，实际 {len(gpu_uuids)} 张")
    online = {g.uuid: g for g in gpu_pool if getattr(g, "status", GpuStatus.ONLINE) == GpuStatus.ONLINE}
    missing = [u for u in gpu_uuids if u not in online]
    if missing:
        raise LLMPodValidationError(f"指定的 GPU 不可用（离线/不存在）：{missing}")
    if copies > 1:
        if nnodes > 1:
            raise LLMPodValidationError("手动选卡暂不支持多副本×跨机（请改用自动调度或逐份单机）")
        pair_manual_replicas(gpu_pool, gpu_uuids, per_copy=parallel)
        return
    # 单副本：整个选集须同 arch（多 arch 见于误选；份间异构请调高副本数）
    bad_arch = [u for u in gpu_uuids if online[u].arch != arch]
    if bad_arch:
        raise LLMPodValidationError(f"指定的 GPU 架构不符（需 {arch}）：{bad_arch}")
    need_per_node = parallel // nnodes
    by_node: dict[str, list[str]] = defaultdict(list)
    for u in gpu_uuids:
        by_node[online[u].node_id].append(u)
    if len(by_node) != nnodes:
        raise LLMPodValidationError(
            f"手动选卡需分布在 {nnodes} 个节点（当前 {len(by_node)}），每节点 {need_per_node} 张"
        )
    bad = {nid: len(us) for nid, us in by_node.items() if len(us) != need_per_node}
    if bad:
        raise LLMPodValidationError(f"每节点需 {need_per_node} 张指定的卡，实际分布 {bad}")


def _validate_manual_selection_shapes(gpu_pool, gpu_uuids, *, shapes: list[tuple[int, int, int]]) -> None:
    """异构副本的手选校验：逐份形状 ``(tp, pp, nnodes)``，总数 = Σ 各份 tp×pp。

    与 ``_validate_manual_selection`` 同口径：重复/在线检查 + 多副本×跨机拒绝 +
    逐份桶配对（``pair_manual_replicas`` 列表形态）。单副本（仅 1 份）沿用单副本
    arch/分布规则。份内跨机 nnodes>1 时按该份口径校验节点分布。
    """
    if not gpu_uuids:
        return
    if len(gpu_uuids) != len(set(gpu_uuids)):
        raise LLMPodValidationError("gpu_uuids 含重复 UUID")
    per_copy = [_cards_per_copy_sh(tp, pp) for tp, pp, _n in shapes]
    total_needed = sum(per_copy)
    if len(gpu_uuids) != total_needed:
        detail = " + ".join(f"{n} 张" for n in per_copy)
        raise LLMPodValidationError(f"手动选卡需 {total_needed} 张（逐份 {detail}），实际 {len(gpu_uuids)} 张")
    online = {g.uuid: g for g in gpu_pool if getattr(g, "status", GpuStatus.ONLINE) == GpuStatus.ONLINE}
    missing = [u for u in gpu_uuids if u not in online]
    if missing:
        raise LLMPodValidationError(f"指定的 GPU 不可用（离线/不存在）：{missing}")
    if len(shapes) > 1:
        if any(n > 1 for _tp, _pp, n in shapes):
            raise LLMPodValidationError("手动选卡暂不支持多副本×跨机（请改用自动调度或逐份单机）")
        pair_manual_replicas(gpu_pool, gpu_uuids, per_copy=per_copy)
        return
    tp, pp, nnodes = shapes[0]
    parallel = max(1, tp) * max(1, pp)
    archs = {online[u].arch for u in gpu_uuids}
    if len(archs) > 1:
        raise LLMPodValidationError(f"单副本所选卡须同架构（单引擎不能跨架构 TP），实际 {sorted(archs)}")
    need_per_node = parallel // max(1, nnodes)
    by_node: dict[str, list[str]] = defaultdict(list)
    for u in gpu_uuids:
        by_node[online[u].node_id].append(u)
    if len(by_node) != max(1, nnodes):
        raise LLMPodValidationError(
            f"手动选卡需分布在 {nnodes} 个节点（当前 {len(by_node)}），每节点 {need_per_node} 张"
        )
    bad = {nid: len(us) for nid, us in by_node.items() if len(us) != need_per_node}
    if bad:
        raise LLMPodValidationError(f"每节点需 {need_per_node} 张指定的卡，实际分布 {bad}")


def validate_manual_gpu_selection(gpu_pool, gpu_uuids, *, shapes: list[tuple[int, int, int]], arch: str) -> None:
    """手选卡校验的统一分流（create 的 assign 与 inspect 预检共用入口）。

    ``shapes`` = 逐份 ``(tp, pp, nnodes)``：create 侧取 instances 中 node_rank=0 的
    展开，inspect 侧取 topology.per_copy_units()。同形副本走单值口径（卡数 =
    tp×pp×份数），异构副本走逐份形状校验（卡数 = Σ 各份 tp×pp）。分流写两遍必
    漂移（历史：inspect 恒走同形口径，把 copies=[{tp:2},{tp:1}] 的合法 3 卡选集
    错判成「需 4 张」——预检红灯、create 放行）。
    """
    if len(set(shapes)) == 1:
        tp, pp, nnodes = shapes[0]
        _validate_manual_selection(
            gpu_pool,
            gpu_uuids,
            tp=tp,
            nnodes=nnodes,
            arch=arch,
            pp=pp,
            copies=len(shapes),
        )
    else:
        _validate_manual_selection_shapes(gpu_pool, gpu_uuids, shapes=shapes)


def pair_manual_replicas(gpu_pool, gpu_uuids, *, per_copy: int | list[int]) -> list[tuple[str, list[str]]]:
    """手选卡多副本分组：按 (node, arch) 桶贪心配份。

    ``per_copy``：int = 每份同卡数（同形副本）；list = 逐份卡数（异构副本，如
    ``[2, 1]`` = 首份 2 卡 + 次份 1 卡）。异构配对从最大份取起、各份从仍够数的最大
    桶取（份内须同桶——同节点同架构由桶定义保证）；任一份配不齐即拒，错误给逐份/
    逐桶余数的人读原因。不按架构预过滤——份间可异构（nvidia 份 + cann 份混选），
    桶键含 arch。同形路径维持原语义：贪心从最大桶取（尽量整份落同节点）；桶余数
    凑不出整份即拒。返回 [(arch, gpu_uuids)]（按份序，异构时与 per_copy 列表序对齐）。
    桶内不区分显存档（同档校验在部署期 ``_tier_uniform``——手选阶段用户点的卡就是意图）。
    """
    if isinstance(per_copy, list):
        return _pair_manual_mixed(gpu_pool, gpu_uuids, per_copy)
    return _pair_manual_uniform(gpu_pool, gpu_uuids, per_copy)


def _buckets(gpu_pool, gpu_uuids) -> dict[tuple[str, str], list[str]]:
    """所选在线卡按 (node, arch) 分桶（桶内 uuid 保持池序稳定）。"""
    by_bucket: dict[tuple[str, str], list[str]] = defaultdict(list)
    for g in gpu_pool:
        if getattr(g, "status", GpuStatus.ONLINE) != GpuStatus.ONLINE:
            continue
        if g.uuid not in gpu_uuids:
            continue
        by_bucket[(getattr(g, "node_id", ""), getattr(g, "arch", ""))].append(g.uuid)
    return by_bucket


def _pair_manual_uniform(gpu_pool, gpu_uuids, per_copy: int) -> list[tuple[str, list[str]]]:
    """同形配对（历史语义原样）：最大桶优先整份取，余数即拒。"""
    by_bucket = _buckets(gpu_pool, gpu_uuids)
    pairs: list[tuple[str, list[str]]] = []
    leftovers: list[tuple[str, str, int]] = []
    buckets = sorted(by_bucket.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for (_nid, bucket_arch), uuids in buckets:
        take = (len(uuids) // per_copy) * per_copy
        for i in range(0, take, per_copy):
            pairs.append((bucket_arch, sorted(uuids[i : i + per_copy])))
        if len(uuids) % per_copy:
            leftovers.append((_nid, bucket_arch, len(uuids) % per_copy))
    if not pairs:
        raise LLMPodValidationError(f"手动选卡凑不出整数副本（每份需同节点同架构 {per_copy} 张）；请调整选择或减少份数")
    if leftovers:
        detail = "、".join(f"{nid[:8]} 余 {n} 张 {a}" for nid, a, n in leftovers)
        raise LLMPodValidationError(
            f"手动选卡有配不齐的余卡：{detail}（每份需同节点同架构 {per_copy} 张）；请取消多余的卡"
        )
    return pairs


def _pair_manual_mixed(gpu_pool, gpu_uuids, per_copy: list[int]) -> list[tuple[str, list[str]]]:
    """异构配对：份按卡数降序取起（大份先占大桶），各份从仍够数的最大桶整份取。

    「最大桶」按桶的总量排（不是剩余量）：倾向把份放进本就最大的同构组，剩余
    尾巴留在小桶——多份混配时这样留下的余卡更可能凑得出下一个整份。
    """
    by_bucket = _buckets(gpu_pool, gpu_uuids)
    picked: set[str] = set()
    order = sorted(range(len(per_copy)), key=lambda i: -per_copy[i])
    result: dict[int, tuple[str, list[str]]] = {}
    for i in order:
        need = per_copy[i]
        # 每轮可用集算一次：够数判定与最终取卡共用（原来两处各过滤一遍）
        avail_by_bucket = {key: [u for u in uuids if u not in picked] for key, uuids in by_bucket.items()}
        cands = sorted(
            ((nid, arch, avail) for (nid, arch), avail in avail_by_bucket.items() if len(avail) >= need),
            # 排序键回看桶总量（by_bucket 原始大小），不看本份拿走后的剩余量
            key=lambda kv: (-len(by_bucket[(kv[0], kv[1])]), kv[0], kv[1]),
        )
        if not cands:
            raise LLMPodValidationError(f"手动选卡配不齐第 {i + 1} 份（需同节点同架构 {need} 张）；请调整选择或份数")
        nid, arch, avail = cands[0]
        chosen = sorted(avail)[:need]
        picked.update(chosen)
        result[i] = (arch, chosen)
    leftover = sorted(set(gpu_uuids) - picked)
    if leftover:
        raise LLMPodValidationError(f"手动选卡有配不齐的余卡：{sorted(leftover)}；请取消多余的卡")
    return [result[i] for i in range(len(per_copy))]


def _validate_node_selection(gpu_pool, node_ids, *, tp: int, nnodes: int, arch: str, pp: int = 1) -> None:
    """校验选节点集：每节点在线 + 同 arch + ≥ tp×pp//nnodes 张可用卡。VRAM 留给 scheduler。

    ``gpu_pool`` 任意带 ``uuid / node_id / arch / status`` 的对象（GpuCandidate / ClusterGpuInfo）。
    与 ``_validate_manual_selection`` 对位：后者校验具体卡，本校验节点级（每节点自动分卡）。
    """
    if not node_ids:
        return
    need_per_node = (tp * pp) // nnodes
    by_node: dict[str, int] = defaultdict(int)
    for g in gpu_pool:
        if getattr(g, "status", GpuStatus.ONLINE) == GpuStatus.ONLINE and getattr(g, "arch", "") == arch:
            by_node[getattr(g, "node_id", None)] += 1
    missing = [n for n in node_ids if n not in by_node]
    if missing:
        raise LLMPodValidationError(f"node_ids 含不可用节点（离线/无 {arch} 卡）：{missing}")
    short = {n: by_node[n] for n in node_ids if by_node[n] < need_per_node}
    if short:
        raise LLMPodValidationError(f"每节点需 {need_per_node} 张 {arch} 可用卡，不足：{short}")


def probe_placement(
    instances,
    gpu_pool,
    *,
    arch: str,
    arch_order: tuple[str, ...] | list[str] | None = None,
    gpu_selector: GpuSelector | None = None,
) -> tuple[bool, str | None]:
    """结构性容量探测：拿真实选卡内核干跑一遍（不预留、不查显存）。

    inspect 预检用。与 ``plan_placement`` 共用同一条 Filter 链（online + arch）与同一套
    阶梯/裁决，只把容量过滤与账本预留摘掉——「显存够不够」由逐卡容量检查
    （``recommend_plan`` / ``assess_feasibility``）另行回答，两件事不混在一个判定里。

    为什么不数卡：计数口径（同 arch 在线卡数 ≥ tp×pp×replicas）表达不了「每个引擎要
    同架构的 tp×pp 张」。tp=2 而 nvidia 空 1 张、cann 空 1 张时数卡说「够」，实际一个
    引擎都凑不出；跨机还要节点互异、同引擎同显存档。这些约束内核已经算全，另写一遍
    计数口径必分叉（分叉的代价是「预检说能放、调度说不行」）。

    ``gpu_selector``：指定节点 / 选节点集 / 手选卡的约束一并生效（否则指定了节点的请求会拿
    整集群去判可行性，给出调度做不到的「可部署」）。
    ``arch_order``：非空时逐引擎回落（异构副本）；**空元组 = 架构闸门淘汰了全部候选**，
    按不可调度处理（不回退到未经闸门的 arch）。返回 ``(能否调度, 不能时的人读原因)``。
    """
    if not instances:
        return True, None
    try:
        plan_placement(
            instances,
            gpu_pool,
            arch=arch,
            gpu_selector=gpu_selector,
            # 结构性探测只看「摆不摆得下」，调度软偏好（本地性/卡痕迹）与它无关——只带回落序
            prefs=PlacementPrefs(arch_order=None if arch_order is None else tuple(arch_order)),
        )
    except LLMPodValidationError as e:
        return False, _probe_reason(instances, gpu_pool, e)
    return True, None


def _probe_reason(instances, gpu_pool, err: LLMPodValidationError) -> str:
    """探测失败的人读原因：需要几份引擎 + 集群在线卡的架构分布。

    用户据此决定「释放哪张卡 / 缩小并行度 / 加卡」，所以先给「哪种卡有几张」，再附内核
    的过滤明细（卡在 Filter 链哪一步被拦下）——只给机械明细或只给总数都不够用。
    """
    if getattr(err, "ladder", "") == "arch_gate":
        return NO_USABLE_ARCH_REASON
    per_engine = {
        key: _cards_per_copy_sh(shards[0].tp, shards[0].pp) for key, shards in group_engines(instances).items()
    }
    by_arch: dict[str, int] = defaultdict(int)
    for g in gpu_pool:
        if getattr(g, "status", GpuStatus.ONLINE) == GpuStatus.ONLINE:
            by_arch[getattr(g, "arch", "")] += 1
    online = "、".join(f"{a} {n} 张" for a, n in sorted(by_arch.items())) or "无在线卡"
    shapes = sorted(set(per_engine.values()))
    shape = f"每份 {shapes[0]} 张同架构卡" if len(shapes) == 1 else "逐份 " + " + ".join(f"{s} 张" for s in shapes)
    rejections = getattr(err, "rejections", {}) or {}
    # 人读原因直接取内核各 filter 的首因（PipelineRejection.reasons），不在此再造映射表
    reasons = getattr(err, "reasons", {}) or {}
    detail = "、".join(f"{reasons.get(name, name)}×{n}" for name, n in sorted(rejections.items()))
    tail = f"；被拦原因 {detail}" if detail else ""
    return (
        f"集群在线卡不足：本部署需 {len(per_engine)} 份引擎（{shape}，共 {sum(per_engine.values())} 张），"
        f"当前在线 {online}{tail}"
    )


def _available_ram(node, ledger) -> float:
    """节点可用内存（GB）：账本口径扣已预留；无账本按总内存 × 内存利用率上限。

    CPU 部署两个入口（容量判定 / 调度 binpack）共用——口径不一致会让预检说能放、
    调度却放不下（或反之）。
    """
    total = float(getattr(node, "memory_gb", 0.0) or 0.0)
    if ledger is None:
        return total * DEFAULT_RAM_UTILIZATION
    return ledger.get_available_ram(getattr(node, "node_id", ""), total)


def assess_cpu_capacity(nodes, *, replicas: int, ram_per_replica_gb: float, ledger=None) -> tuple[bool, str | None]:
    """无状态判集群能否放下 replicas 份纯 CPU 实例（按节点内存 binpack，与调度同源）。

    ``nodes`` 元素需带 ``node_id`` 与 ``memory_gb``（节点总内存）；``ledger`` 非空时
    减去已预留内存（集中 claim 口径，与 ResourceLedger 同源）。

    与 :func:`plan_cpu_placement` 共用同一套调度逻辑（同节点可承载多份）——两边各写一套
    会让 inspect 说放得下、调度却抛错（或反之）。
    """
    if not nodes:
        return False, "集群无在线节点，无法调度 CPU 部署"
    try:
        _plan_cpu_by_ids(
            [f"probe-{i}" for i in range(max(1, replicas))],
            nodes,
            ram_per_instance_gb=max(0.0, ram_per_replica_gb),
            ledger=ledger,
        )
    except LLMPodValidationError as e:
        return False, str(e)
    return True, None


def plan_cpu_placement(
    instances, nodes, *, ram_per_instance_gb: float, ledger=None, force: bool = False
) -> dict[str, str]:
    """纯 CPU / 全内存档的调度：按可用内存 binpack（best-fit）给每个实例选节点。

    返回 ``{instance_id: node_id}``；某实例无处可放 → raise ``LLMPodValidationError``
    （调用方回滚已预留内存）。``ledger`` 参与可用量计算（集中 claim），本函数**不预留**
    （预留由调用方提交，失败统一回滚）。``force=True`` 时无处可放不拒：退化为「可用内存
    最大的节点」超额调度（能否跑起来由引擎/OOM 兜底，与显存侧 ``force_reserve`` 同口径）。
    """
    return _plan_cpu_by_ids(
        [i.instance_id for i in instances],
        nodes,
        ram_per_instance_gb=ram_per_instance_gb,
        ledger=ledger,
        force=force,
    )


def recommend_cpu_placement(instances, nodes, *, ram_per_instance_gb: float, ledger=None) -> list[str] | None:
    """inspect 推荐纯 CPU 调度的干跑壳：跑一遍创建壳 ``plan_cpu_placement``（不预留）。

    与显存侧 ``recommend_plan`` → ``plan_placement`` 同款委托，入参同样收真实实例
    列表（展开产物，逐实例一份内存）；实例此刻的 ``instance_id`` 还是空串（正式 id
    创建期才由 _plan 盖），这里就地盖临时探针 id 作 binpack 键。返回逐实例节点列表
    （序 = 实例序）；放不下返回 ``None``——容量原因由 ``assess_cpu_capacity`` 侧
    负责报，此处不重复。
    """
    for i, inst in enumerate(instances):
        inst.instance_id = f"preview-{i}"
    try:
        picks = plan_cpu_placement(instances, nodes, ram_per_instance_gb=ram_per_instance_gb, ledger=ledger)
    except LLMPodValidationError:
        return None
    return list(picks.values())


def _plan_cpu_by_ids(
    instance_ids: list[str], nodes, *, ram_per_instance_gb: float, ledger=None, force: bool = False
) -> dict[str, str]:
    """binpack 调度内核：按实例 id 列表逐个选节点（``plan_cpu_placement`` 与容量判定共用）。"""
    remaining = {getattr(n, "node_id", ""): _available_ram(n, ledger) for n in nodes}
    out: dict[str, str] = {}
    for iid in instance_ids:
        need = ram_per_instance_gb
        # best-fit：能放下且余量最小的节点（先填小节点，把大节点留给后续）
        candidates = sorted(
            ((avail, nid) for nid, avail in remaining.items() if avail + 1e-9 >= need),
            key=lambda item: (item[0], item[1]),
        )
        if not candidates:
            if not force:
                raise LLMPodValidationError(
                    f"无节点内存可放下该实例（需 {need:.1f}GB：未 offload 权重 + KV + 系统保留）"
                )
            if not remaining:
                raise LLMPodValidationError("无在线节点可做纯 CPU 部署")
            # 强制调度：取可用内存最大的节点（超额记账，账本走 force_reserve）
            nid = max(remaining.items(), key=lambda item: (item[1], item[0]))[0]
        else:
            _, nid = candidates[0]
        remaining[nid] -= need
        out[iid] = nid
    return out


class PlacementPick(NamedTuple):
    """单分片选卡结果（plan_placement 的元素；一个引擎的多机分片各一条）。"""

    instance: InstanceSpec
    node_id: str
    cards: list


def group_engines(instances) -> dict[str, list]:
    """按引擎实例分组（多机引擎的分片同组，节点组一次选位；dict 保序 = 展开序）。

    无 ``engine_instance_id`` 的旧调用形态回退按 replica_idx 分组（同 replica = 同引擎）。
    选卡 fold 与「为什么放不下」的诊断共用本分组口径——各写一份会让诊断里的份数/卡数
    与实际调度尝试的份数不一致。
    """
    engines: dict[str, list] = defaultdict(list)
    for inst in instances:
        engines[str(inst.engine_instance_id or f"replica-{inst.replica_idx}")].append(inst)
    return engines


def card_reserve(g, vram_percent: float | None) -> float:
    """该卡本次要预留的显存量：显存占比设定 → %×total；否则整卡。

    无显存占比时引擎按默认 ``--gpu-memory-utilization`` 抢占整卡（KV cache 填满
    total×util），预留须按整卡容量对齐，否则账本误判余量、并发部署堆同一卡 → 实际 OOM。
    """
    if vram_percent:
        return vram_percent * g.total_vram_gb
    return g.total_vram_gb * DEFAULT_GPU_MEMORY_UTILIZATION


def physical_fits(g, reserve_gb: float, need_gb: float | None) -> bool:
    """心跳实况物理闸：``total − used_vram_gb`` 装得下本次预留。

    心跳缺失（``used_vram_gb is None``，管理端刚重启 / 未上报）时跳过——可用性优先，
    账本闸与 staging 复核仍在。首建 assign 与 restart 原卡再预留共用本判据，
    两处手写会漂移成「首建拦、重启放」。
    """
    used = getattr(g, "used_vram_gb", None)
    if used is None:
        return True
    return reserve_fits(reserve_gb, need_gb, g.total_vram_gb - used)


def reserve_fits(reserve_gb: float, need_gb: float | None, available_gb: float) -> bool:
    """一次新预留能否落到这张卡上（容量判据唯一定义处）。

    ``reserve_gb`` 是本次要向卡要的预留量（:func:`card_reserve`）；``need_gb`` 是模型在该卡
    上的每卡需求（判「占比上限装不装得下模型」，None = 未知/不判）；``available_gb`` 是卡的
    可用显存——由调用方决定是账本可用量还是心跳物理可用量（两个数据源，同一条判据）。

    账本侧（:meth:`GpuScheduler.card_fits`）与心跳侧（``recommend_plan`` 的预览谓词）都用它，
    两处手写会让「预览说能落、create 报无可用卡」。
    """
    if need_gb is not None and reserve_gb + 1e-6 < need_gb:
        return False
    return available_gb + 1e-6 >= reserve_gb


def plan_placement(
    instances,
    gpu_pool,
    *,
    arch: str,
    card_ok=None,
    gpu_selector: GpuSelector | None = None,
    prefs: PlacementPrefs | None = None,
    preused_uuids: set[str] | None = None,
):
    """无状态多引擎选卡 fold——assign 与 recommend_plan 共用的编排内核。

    外层保留部署级联合约束：本部署已选卡排重（used_uuids 跨引擎全局）、同副本多机
    节点互异（used_nodes_by_replica）、VRAM 预留原子性（先整体规划，预留由 assign 提交）。
    逐引擎（engine_instance_id 分组，多机引擎含全部分片）调 :func:`pipeline.place_engine`
    选卡：单节点引擎走 single_node 阶梯，多机引擎走 cross_node 阶梯（等卡数节点组）；
    分片按 node_rank 与调度节点配对（head 在前）。``prefs``：调度偏好与架构回落序
    （见 :class:`PlacementPrefs`；缺省 = 无偏好、单架构）。``preused_uuids``：调用方语境里
    已被占用的卡（如在线扩容时存量副本的卡），进入全局排重起点。
    ``card_ok``：单一 Callable（同形——全部署一个每卡需求）或 ``{replica_idx(str): Callable}``
    （异构副本逐份容量口径，取不到的份退无容量过滤）。不预留、不改 instance、不查
    ledger。返回 :class:`PlacementPick` 列表；选不出抛 LLMPodValidationError（assign
    捕获后释放 ledger）。
    """
    prefs = prefs or PlacementPrefs()
    used_uuids: set[str] = set(preused_uuids or ())
    used_nodes_by_replica: dict[str, set[str]] = defaultdict(set)
    target_node = gpu_selector.node_id if gpu_selector else None
    target_nodes = frozenset(gpu_selector.node_ids) if (gpu_selector and gpu_selector.node_ids) else None
    wanted_uuids = tuple(gpu_selector.gpu_uuids) if (gpu_selector and gpu_selector.gpu_uuids) else None

    engines = group_engines(instances)

    # 手选 + 多副本：调度顺序按「每份卡数降序」（与校验期 _pair_manual_mixed 同款
    # 大份优先贪心）——调度若按 copies 声明序逐份取最大桶，小份会先把大节点上的
    # 卡抓走，同一选集能否成功取决于填写顺序；非手选路径保持展开序（层级偏好/本地性
    # 对声明序无感）
    engine_items = list(engines.items())
    if wanted_uuids is not None and len(engine_items) > 1:
        engine_items.sort(key=lambda kv: -_cards_per_copy_sh(kv[1][0].tp, kv[1][0].pp))

    plan: list[PlacementPick] = []
    for eng_key, shards in engine_items:  # 手选多副本：大份优先；其余保展开序
        shards = sorted(shards, key=lambda s: (s.node_rank, s.rank))
        lead = shards[0]
        nnodes = max(1, int(lead.nnodes or 1))
        need = _cards_per_copy_sh(lead.tp, lead.pp) // nnodes
        if gpu_selector and gpu_selector.node_id and nnodes > 1:
            # validate_topology 已在入口拒绝（防御：直调路径兜底）；gpu_uuids 跨机放行
            raise LLMPodValidationError("跨机部署不支持指定单个节点（用 gpu_uuids 指定跨机卡）")
        replica_key = str(lead.replica_idx)
        engine_card_ok = card_ok.get(replica_key) if isinstance(card_ok, dict) else card_ok
        req = EngineRequest(
            engine_id=eng_key,
            need_per_node=need,
            nnodes=nnodes,
            arch=arch,
            used_uuids=frozenset(used_uuids),
            exclude_nodes=frozenset(used_nodes_by_replica[replica_key]) if nnodes > 1 else frozenset(),
            target_node=target_node,
            target_nodes=target_nodes,
            wanted_uuids=wanted_uuids,
            locality_node_ids=prefs.locality_node_ids,
            card_state=prefs.card_state,
            # 自动路径逐 arch 试探（默认 arch 放不下落候选序后面的；异构副本用）；
            # 手选路径 place_engine 走 manual 早退，arch_order 不生效。
            # None（不给候选序）与空元组（给了但没有候选）语义不同，见 EngineRequest
            arch_order=prefs.arch_order,
        )
        placement = place_engine(gpu_pool, req, card_ok=engine_card_ok)
        for shard, (node_id, cards) in zip(shards, placement.shards):
            used_uuids.update(getattr(g, "uuid", "") for g in cards)
            if nnodes > 1:
                used_nodes_by_replica[replica_key].add(node_id)
            plan.append(PlacementPick(instance=shard, node_id=node_id, cards=cards))

    # 手选集收口：所有引擎放完后 wanted 须被整体覆盖——单引擎=本引擎全消费；多副本
    # 手选=每引擎一份。剩卡说明选集与拓扑对不上（数量错，或有卡被 Filter 链拦下）
    if wanted_uuids:
        leftover = set(wanted_uuids) - used_uuids
        if leftover:
            raise LLMPodValidationError(f"指定 GPU 不可用（离线/架构不符/显存不足）：{sorted(leftover)}")
    return plan


def recommend_plan(
    instances,
    gpu_pool,
    *,
    arch: str,
    required_vram_gb: float | None = None,
    per_replica_vram: dict[str, float] | None = None,
    prefs: PlacementPrefs | None = None,
    vram_utilization: float | None = None,
) -> list[PlacementPick] | None:
    """inspect 推荐选卡的内核：跑一遍 plan_placement，保留逐实例调度结果供逐份分组。

    与 ``GpuScheduler.assign`` 走同一选卡内核，唯一差异是 ``card_ok`` 的数据源——assign
    用账本，本函数用 ``cluster_gpus.available_vram_gb``（= total − 心跳实时占用）。容量
    判据都走 :func:`reserve_fits`：**本次预留**（``vram_utilization`` × total，缺省整卡
    0.9）既要装得下模型每卡需求、又不得超出可用——两边各写一套会出现「预览说能落、
    create 报无可用卡」。

    ``required_vram_gb``：同形口径（单值，按首实例并行度摊到每卡）；``per_replica_vram``
    （replica_idx str → 每卡需求）：异构副本逐份口径，存在时优先——每引擎实例按所属
    份的每卡需求过容量过滤（key 取不到的份退无过滤，与 assign 同语义）。``prefs``：
    与 assign 同义的调度偏好与架构回落序（预览给出的调度位置才是 create 会调度到的位置）。
    """
    if not instances:
        return None

    inst0 = instances[0]
    parallel = _cards_per_copy_sh(inst0.tp, inst0.pp)
    per_card = (required_vram_gb / parallel) if required_vram_gb else None

    def _buffer_card_ok(need: float | None):
        """该卡能否承载一次新预留（:func:`reserve_fits`，可用量换成心跳物理可用）。"""

        def _ok(g) -> bool:
            reserve = card_reserve(g, vram_utilization)
            available = getattr(g, "available_vram_gb", 0.0) or 0.0
            return reserve_fits(reserve, need, available)

        return _ok

    if per_replica_vram:
        card_ok = {rid: (_buffer_card_ok(v) if v else None) for rid, v in per_replica_vram.items()}
    else:
        card_ok = _buffer_card_ok(per_card) if per_card is not None else None
    try:
        return plan_placement(instances, gpu_pool, arch=arch, card_ok=card_ok, gpu_selector=None, prefs=prefs)
    except LLMPodValidationError:
        return None


def recommend_placement(
    instances, gpu_pool, *, arch: str, required_vram_gb: float | None = None
) -> tuple[list[str], list[str]]:
    """inspect 推荐选卡的扁平视图：(gpu_uuids, node_ids)；选不出返 ([], [])。

    完整调度结果（逐实例分组等）用 :func:`recommend_plan`——生产路径（inspect 路由）直接用
    后者，本函数只是给调用方要扁平结果的便捷视图。
    """
    plan = recommend_plan(instances, gpu_pool, arch=arch, required_vram_gb=required_vram_gb)
    if plan is None:
        return [], []
    uuids: list[str] = []
    node_ids: list[str] = []
    for _inst, node_id, cards in plan:
        if node_id not in node_ids:
            node_ids.append(node_id)
        uuids.extend(getattr(g, "uuid", "") for g in cards)
    return uuids, node_ids


class GpuScheduler:
    """GPU 调度器。"""

    def __init__(self, ledger: ResourceLedger):
        self.ledger = ledger

    def card_fits(self, g, vram_per_gpu_gb: float, vram_percent: float | None = None) -> bool:
        """该卡能否承载一次新预留（容量口径唯一定义处）。

        两条闸门都要过，各自成立、不叠加：

        - **物理可用**（心跳实况）：``total − used_vram_gb`` 须装得下本次预留。外部进程
          （非本平台的容器）占着的卡在这里被拦下——账本看不见那部分占用，只查账本会
          放行到调度、容器起来即 OOM。这里不再乘利用率上限：``reserve`` 已经含了上限
          （整卡独占 = util×total），心跳 ``used`` 是物理量，再折一次会额外收紧一个
          0.9（同一张卡被两套折算判两次）。**代价**：本底占用超过 ``(1−util)×total``
          （默认 0.9 → 10%）的卡一律判满，出路是调低生效显存占比（把 ``reserve`` 降下来）
          或 ``force``——模型需求本身不参与这一步，因为引擎是按占比要预留的。
        - **账本可用**：``total×util − 已预留`` 须装得下，它是「已规划未起」的权威来源
          （staging 中的部署在心跳里还看不到）。

        assign 的 capacity Filter 与 ``preview_scale`` 的可行性探测共用本谓词——预览与
        真实调度同口径，不会出现「预览说能扩、assign 阶段 reserve 失败」。
        """
        reserve = card_reserve(g, vram_percent)
        if not physical_fits(g, reserve, vram_per_gpu_gb):
            return False  # 卡上已有占用（含平台外进程），再放一份进去会 OOM
        return reserve_fits(reserve, vram_per_gpu_gb, self.ledger.get_available(g.uuid, g.total_vram_gb))

    async def assign(
        self,
        deployment_id: str,
        instances: list[InstanceSpec],
        gpu_pool: list[GpuCandidate],
        vram_per_gpu_gb: float,
        arch: str = "nvidia",
        gpu_selector: GpuSelector | None = None,
        vram_percent: float | None = None,
        preused_uuids: set[str] | None = None,
        prefs: PlacementPrefs | None = None,
        force: bool = False,
        per_replica_vram: dict[str, float] | None = None,
    ) -> list[GpuAssignment]:
        """给每个 instance 分配 node + GPU（跨机按节点均分），预留 VRAM。

        选卡委托无状态内核 ``plan_placement``（与 inspect 的 recommend_placement 共用同一挑选规则），
        本方法只负责有状态提交：VRAM 预留 + instance 写回。gpu_pool: 在线 GPU；就地写
        instance.assigned_node / assigned_gpus / assigned_gpu_uuids。任一实例选不出 / 预留失败 →
        整体回滚（release 幂等，重复调用无害）。``vram_percent``：生效显存占比（编排器从
        用户引擎 flag 解析出的唯一定义处，None=整卡独占）。``preused_uuids``：本部署存量已占卡
        （在线扩容场景），选卡排重起点，不计入本次预留。``per_replica_vram``
        （replica_idx str → 每卡需求）：异构副本逐份容量口径，存在时替代单值
        ``vram_per_gpu_gb`` 参与容量过滤与预留记账（取不到的份退单值）。

        ``prefs``：调度偏好与逐引擎架构回落序（见 :class:`PlacementPrefs`——异构副本逐份
        回落 / 本地性 / 卡痕迹），与 inspect 预览同源，否则「预览说落这儿、create 落别处」。

        ``force``：强制部署——跳过容量过滤（``card_ok`` 不注入，显存不足的卡仍调度）并改走
        ``force_reserve``（不计容量直接记账，超额由引擎/NCCL 最终裁决 OOM）。结构校验
        （在线/架构/分布）不受 force 影响，仍硬拒。非 force 时容量过滤含**物理可用**闸
        （心跳实况：卡上已有占用——含平台外进程——则拒），见 :meth:`card_fits`。
        """
        # 手动选卡 / 选节点集预校验（结构 + 在线 + arch + 分布；VRAM 留给 reserve 兜底）。
        # shapes 取 instances 中 node_rank=0 的逐份形状（多机份每节点一个实例，只取首秩）；
        # 同形/异构分流在 validate_manual_gpu_selection（与 inspect 预检共用）
        if gpu_selector and gpu_selector.gpu_uuids and instances:
            shapes = [
                (max(1, int(i.tp or 1)), max(1, int(i.pp or 1)), max(1, int(i.nnodes or 1)))
                for i in instances
                if int(i.node_rank or 0) == 0
            ]
            validate_manual_gpu_selection(gpu_pool, gpu_selector.gpu_uuids, shapes=shapes, arch=arch)
        if gpu_selector and gpu_selector.node_ids and instances:
            inst0 = instances[0]
            _validate_node_selection(
                gpu_pool,
                gpu_selector.node_ids,
                tp=max(1, int(inst0.tp or 1)),
                nnodes=max(1, int(inst0.nnodes or 1)),
                arch=arch,
                pp=max(1, int(inst0.pp or 1)),
            )

        # 选卡（无状态内核）——card_ok 用 ledger 版（乐观预留口径）。选卡是纯规划、
        # 尚无任何预留，失败直接上抛；真正的回滚在下方有状态提交段（只释放本次挑选
        # 的卡——在线扩容时存量预留不连带放掉）
        def _vram_of(inst) -> float:
            """该实例所属份的每卡需求（异构逐份；同形/取不到退单值）。"""
            if per_replica_vram:
                return per_replica_vram.get(str(int(inst.replica_idx or 0)), vram_per_gpu_gb)
            return vram_per_gpu_gb

        card_ok: object = None if force else lambda g, _inst=None: self.card_fits(g, vram_per_gpu_gb, vram_percent)
        if not force and per_replica_vram:
            # 异构逐份容量过滤：每份各自口径（plan_placement 按 replica_idx 取本份的谓词）
            card_ok = {}
            for i in instances:
                if int(i.node_rank or 0) != 0:
                    continue
                need = _vram_of(i)
                # 默认参数固定本份的 need：直接闭包会晚绑定到最后一份的值
                card_ok[str(int(i.replica_idx or 0))] = lambda g, need=need: self.card_fits(g, need, vram_percent)
        plan = plan_placement(
            instances,
            gpu_pool,
            arch=arch,
            # force 不注入容量过滤（card_ok=None → Filter 链无 capacity 节点）：
            # 显存不足的手选/自动卡仍调度，由引擎/NCCL 最终裁决 OOM
            card_ok=card_ok,
            gpu_selector=gpu_selector,
            preused_uuids=preused_uuids,
            prefs=prefs,
        )

        # 有状态提交：逐卡预留 + instance 写回。本部署各 instance 选中的卡互不重叠（plan_placement
        # 已用 used_uuids 排重），故先整体选完再预留与「边选边预留」结果一致。
        result: list[GpuAssignment] = []
        picked_cards: set[str] = set()
        try:
            for inst, node_id, cards in plan:
                inst_vram = _vram_of(inst)
                picked_allocated: dict[str, float] = {}
                for g in cards:
                    reserve_gb = card_reserve(g, vram_percent)
                    if force:
                        # 强制预留：跳过容量校验直接记账（超额由用户承担，与 restart force 同口径）
                        await self.ledger.force_reserve(deployment_id, g.uuid, reserve_gb)
                    else:
                        ok = await self.ledger.reserve(deployment_id, g.uuid, reserve_gb, g.total_vram_gb)
                        if not ok:
                            raise LLMPodValidationError(
                                f"显存预留失败 gpu={g.uuid} "
                                f"（需 {reserve_gb:.1f}GB，可用 {self.ledger.get_available(g.uuid, g.total_vram_gb):.1f}GB）"
                            )
                    picked_allocated[g.uuid] = reserve_gb
                    picked_cards.add(g.uuid)
                inst.assigned_node = node_id
                inst.assigned_gpus = [g.index for g in cards]
                inst.assigned_gpu_uuids = [g.uuid for g in cards]
                # 实际部署 arch 逐实例随调度到的卡（异构多副本各份各的：cann 份解析 cann
                # 镜像）；卡无 arch 信息时回退调用方给的部署 arch
                if cards:
                    inst.gpu_arch = getattr(cards[0], "arch", None) or arch
                result.append(
                    GpuAssignment(
                        instance_id=inst.instance_id,
                        node_id=node_id,
                        gpu_uuids=[g.uuid for g in cards],
                        vram_gb=inst_vram,
                        gpu_memory_utilization=vram_percent,
                        allocated=picked_allocated,
                    )
                )
                log.info("assigned instance=%s node=%s gpus=%s", inst.instance_id or "?", node_id, inst.assigned_gpus)
        except LLMPodValidationError:
            await self.ledger.release_cards(deployment_id, picked_cards)
            raise
        return result
