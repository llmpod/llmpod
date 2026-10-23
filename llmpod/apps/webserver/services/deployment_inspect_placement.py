"""部署预检的推荐调度段：自动/手选选卡 dry-run + 逐份 replica_plans + 放不下归因。

从路由函数整体迁入（纯搬移：判据、文案、issue 顺序不变）。主函数 ``build_recommended_placement``
负责池准备与装配，逐段子函数分工：

    _pair_manual_plans     手选路径：pair_manual_replicas 分组配对 → 逐份 ReplicaPlan；
                           顶层调度字段同时取选集（不再跑自动内核）
    _run_selection_kernel  自动路径内核：recommend_plan 一次跑（arch 回落），顶层调度字段
    _auto_replica_plans    自动路径逐份重排：按 InstanceSpec.replica_idx 分组（无新调度调用）
    _worst_margin          逐卡余量（available − 每卡所需；fit 判据与 create 同双闸），两路径共用
    _probe_structural      结构性探测（真实内核干跑，不查显存）
    _attribute_empty_placement  推荐调度为空时的五分支原因归因
    _build_cpu_placement   纯 CPU 部署的节点级调度（同 binpack 内核 dry-run，无卡）

段间共享的中间量（free_pool / rec_uuids / req_vram / per_copy_need）经显式参数传递，
不进 InspectState——只在本段内流转。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager.deployment.orchestrator import get_orchestrator
from llmpod.apps.manager.deployment.scheduler.gpu import (
    card_reserve,
    pair_manual_replicas,
    probe_placement,
    recommend_cpu_placement,
    recommend_plan,
    reserve_fits,
)
from llmpod.apps.manager.deployment.scheduler.topology import cpu_lane_pins, unit_parallel, validate_cpu_lane
from llmpod.apps.webserver.schemas.deployment_inspect import (
    RecommendedCard,
    RecommendedPlacement,
    ReplicaPlan,
    UnitPlan,
    UnitPlacement,
)
from llmpod.services import deployment_service
from llmpod.core.exceptions import LLMPodValidationError

if TYPE_CHECKING:
    from llmpod.apps.webserver.services.deployment_inspect import InspectState

log = logging.getLogger("llmpod.apps.webserver.services.deployment_inspect_placement")


async def build_recommended_placement(db: AsyncSession, c: InspectState) -> None:
    """推荐选卡（dry-run，给前端默认选哪几张卡 + 逐份 replica_plans）+ 放不下归因。

    依赖 state：request / model / adapter / topo / cluster_gpus / node_rows / card_status /
    resolved / memory / arch / arch_candidates / cpu_only / manual / sel_uuids /
    manual_gpus / engine。填充：recommended_placement。
    可能追加 issue：结构性探测原因、放不下五分支归因（均阻断级）。

    与部署期 assign 走同一选卡内核（expand_topology → plan_placement）。推荐 arch 优先
    adapter 默认；默认 arch 无合格卡（如全 Ascend 集群 + vllm 默认 nvidia）则退回其它
    受支持 arch——前端按 gpu_uuids 预选后即成手选，部署期 arch 随所选卡推断，前后一致。
    只推零占用 + 引擎可用卡：被（active 部署部分）占用的卡绝不自动推荐，一卡多部署只能用户手选。

    纯 CPU 部署不出选卡，改出节点级调度（_build_cpu_placement）——无 GPU 集群 / 显存池
    耗尽落到 CPU 档时，那是响应里唯一的调度信息。CPU 意图按**请求偏好**判（显式
    cpu_only），不用 c.cpu_only（= CPU 档解出）：通道解不出时（指定节点离线/内存不够/
    缺元数据 → memory=None）后者恒 False，显式 CPU 请求会错走 GPU 内核、把 node_ids
    当显卡候选集给出调度结果（前端看非空就不拦，纯 CPU 意图一路推到提交才被 create 拒）。
    """
    request, model, adapter, topo = c.request, c.model, c.adapter, c.topo
    cluster_gpus = c.cluster_gpus
    cpu_only_intent = request.offload_preference == resource.SCENARIO_CPU_ONLY
    if cpu_only_intent or c.cpu_only:
        # 纯 CPU 部署无卡可推：仍回对象而非 None（前端按对象形状消费，契约不变）；
        # gpu_uuids 恒空——前端若当手选卡回传，与 allow_cpu 互斥守卫直接 422 对撞
        _build_cpu_placement(c)
        return
    if not cluster_gpus:
        return

    # ---- 池准备：偏好 / 本地性 / 空闲池 / 逐份每卡需求 ----
    rec_instances = adapter.expand_topology(topo)
    # 调度偏好与 create/扩容同源装配（同一入口 = 预检给出的调度结果就是 create 实际会调度到的位置）；
    # 卡现状上面已查过，传进来复用
    prefs = await deployment_service.placement_prefs(
        db, model_id=model.id, pool=cluster_gpus, arch_order=c.arch_candidates, card_status=c.card_status
    )
    # 本地已有该模型的节点（同一份偏好里的集合，回显到卡上供用户挑调度位置）
    for cg in cluster_gpus:
        cg.model_local = cg.node_id in prefs.locality_node_ids
    req_vram = cluster_gpus[0].required_vram_gb
    # 指定节点（未手选卡）时推荐池收进指定节点——否则会推荐指定范围外的卡，
    # 前端采纳后 create 在指定节点内找不到卡，预览与创建对撞
    pinned_nodes: set | None = None
    if request.gpu_selector and not c.sel_uuids:
        pinned_nodes = (
            set(
                [request.gpu_selector.node_id]
                if request.gpu_selector.node_id
                else (request.gpu_selector.node_ids or [])
            )
            or None
        )
    free_pool = [
        cg
        for cg in cluster_gpus
        if not cg.occupied_by and cg.grey_reason is None and (pinned_nodes is None or cg.node_id in pinned_nodes)
    ]
    per_gpu_need = None
    if req_vram is not None:
        per_gpu_need = req_vram / max(1, topo.gpus_per_engine())
    # 异构副本逐份每卡需求（同形退单值口径；推导域冻结：异构只来自用户显式请求）
    per_copy_units = topo.per_copy_units()
    per_copy_need: list = [(req_vram / max(1, u.tp * u.pp)) if req_vram is not None else None for u in per_copy_units]
    per_replica_vram = (
        {str(i): v for i, v in enumerate(per_copy_need) if v is not None} if not topo.is_homogeneous else None
    )

    # 逐卡 uuid→卡 映射建一次，各子函数共用（勿每次重建）
    by_uuid = {cg.uuid: cg for cg in cluster_gpus}

    # ---- 逐份主结构：手选 → 分组配对；自动 → 内核 + 按 replica_idx 重排 ----
    replica_plans: list[ReplicaPlan] = []
    rec_plan = None  # 自动路径才有；手选路径探测段按 c.manual 跳过
    unit_plans = [
        UnitPlan(
            role=u.role,
            tp=u.tp,
            pp=u.pp,
            ep=u.ep,
            nnodes=u.nnodes,
            gpus_per_engine=u.gpus_per_engine(),
            image=c.resolved.image if c.resolved is not None else None,
        )
        for u in (topo.units or [])
    ]
    if c.sel_uuids:
        replica_plans = _pair_manual_plans(
            cluster_gpus, c.sel_uuids, topo, per_copy_units, per_copy_need, per_gpu_need, by_uuid
        )
        # 手选路径顶层调度字段 = 选集本身（去重有序）。顶层字段的设计用途是给前端回填预选，
        # 取自动内核结果会把用户手选的卡静默换成内核挑的卡，与 replica_plans（描述选集）
        # 对撞；也不必跑内核——其结果在手选路径只进顶层字段
        rec_uuids = list(dict.fromkeys(c.sel_uuids))
        sel_nodes = sorted({by_uuid[u].node_id for u in rec_uuids if u in by_uuid})
        # 部署级 arch 回显取首张所选卡（异构混选时逐份架构见 replica_plans）
        rec_arch = next((by_uuid[u].arch for u in rec_uuids if u in by_uuid), c.arch)
    else:
        rec_plan, rec_uuids, sel_nodes, rec_arch = _run_selection_kernel(
            c, rec_instances, free_pool, req_vram, per_replica_vram, prefs, by_uuid
        )
        if rec_plan is not None:
            replica_plans.extend(_auto_replica_plans(rec_plan, by_uuid, per_copy_need, per_gpu_need, rec_arch))

    # ---- 顶层装配：arch 统计 + 卡现状快照 ----
    arch_summary: dict = {}
    for rp in replica_plans:
        arch_summary[rp.arch] = arch_summary.get(rp.arch, 0) + sum(len(u.gpu_uuids) for u in rp.units)
    hostname_by_node = {n.id: (n.hostname or "") for n in c.node_rows}
    c.recommended_placement = RecommendedPlacement(
        gpu_uuids=rec_uuids,
        node_ids=sel_nodes,
        arch=rec_arch,
        total_gpus=topo.total_gpus(),
        arch_summary=arch_summary,
        unit_plans=unit_plans,
        replica_plans=replica_plans,
        # 被推荐卡的现状快照（手选路径同为选集）：用户据此判断要不要用这张卡
        cards=[
            RecommendedCard(
                uuid=cg.uuid,
                node_id=cg.node_id,
                hostname=hostname_by_node.get(cg.node_id, ""),
                arch=cg.arch,
                index=cg.index,
                total_vram_gb=cg.total_vram_gb,
                available_vram_gb=cg.available_vram_gb,
                required_vram_gb=cg.required_vram_gb,
                card_state=cg.card_state,
                model_local=cg.model_local,
                occupants=cg.occupants,
            )
            for cg in (by_uuid.get(u) for u in rec_uuids)
            if cg is not None
        ],
    )

    # ---- 放不下归因：结构性探测与容量分流出其一 ----
    structural_failed = _probe_structural(c, rec_instances, rec_plan)
    _attribute_empty_placement(c, free_pool, rec_uuids, req_vram, structural_failed)


def _build_cpu_placement(c: InspectState) -> None:
    """纯 CPU 部署的推荐调度（节点级）：逐副本指定时按指定编排回显，否则干跑 binpack。

    输入与创建路径对齐（预检给出的调度结果 = create 实际会调度到的位置）：实例数取拓扑展开、
    每实例内存取推荐档（与 _solve_lane 判容量同档）、节点池 lane_node_resources（指定节点
    已收窄）、账本同源。gpu_uuids 恒空（前端当手选卡回传会撞 allow_cpu 互斥守卫）。

    CPU 形态非法（validate_cpu_lane 已在 _solve_lane 判红）或容量装不下时回空
    调度结果。**容量类阻断（指定节点装不下）可被 force 绕过**——此时照常给调度结果并
    允许推进（前端确认页的「强制部署」入口才有触达路径），与显存侧
    ``test_inspect_cpu_lane`` 约定的「红灯不给调度结果」并存：这里的取舍是指定编排
    本身无歧义（用户指定），真正要拦的是选不出合法解的自动路径。binpack 在
    容量判定里已跑一次、此处再跑一遍：容量判定必须容忍 CPU 形态非法的拓扑
    （形态先判红、调度后判空，两段各判各的），复用结果要把两段判定拧在一起，
    且两次都是微秒级，不值。
    """
    # 结构问题转 issue 不 422（与 _dry_run_resolve 同口径）。CPU 形态闸是 validate_cpu_lane
    # ——tp>1 / 异构只有它拦（expand 内嵌的 validate_topology 拦不住，只有跨机类重叠），
    # 删掉这行 tp>1 的纯 CPU 请求就会拿到绿灯调度结果、与 _solve_lane 的阻断 issue 自相矛盾。
    # 抑制调度结果的红灯分两类：形态非法（expand 抛出）；指定节点不在线（求解域被 _solve_lane
    # 清空——离线指定的 issues 里已有「不在线」，调度无意义）。
    # **容量红（cpu_pins_over）不抑制**——这类阻断可被 force 绕过，调度结果照给但份级
    # fit=false：空调度结果会被前端第二步硬拦，「强制部署」入口（确认页）就永远够不着；
    # 自动路径容量红天然 picks=None 回空，不受此影响
    pinned_nodes = cpu_lane_pins(c.request.gpu_selector, c.request.offload_preference)
    pins_offline = pinned_nodes is not None and not c.lane_node_resources
    structural_blocked = not c.deployable and not c.cpu_pins_over
    if pins_offline or structural_blocked:
        instances = []
    else:
        try:
            validate_cpu_lane(c.topo)
            instances = c.adapter.expand_topology(c.topo)
        except LLMPodValidationError:
            instances = []
    picks = None
    if instances and pinned_nodes is not None:
        picks = pinned_nodes if len(pinned_nodes) == len(instances) else None
    elif instances:
        picks = recommend_cpu_placement(
            instances,
            c.lane_node_resources,
            # 推荐档（recommended_tier）的每实例内存需求，与 _solve_lane 判容量同档
            ram_per_instance_gb=c.memory.tiers[c.memory.recommended_tier].ram_gb,
            ledger=get_orchestrator().ledger,
        )
    replica_plans: list[ReplicaPlan] = []
    node_ids: list[str] = []
    if picks:
        node_ids = list(dict.fromkeys(picks))
        # CPU 形态已验过恒同构、tp=pp=nnodes=1：逐实例即逐份（一实例一份），无需再分组
        for seq, (inst, node_id) in enumerate(zip(instances, picks)):
            # 份级 fit 只看本份调度是否超载（cpu_pins_over 已按指定编排逐节点判过）；
            # 不叠 c.deployable——它被任一节点超载拉 False，会把健康节点上的份也标红
            plan_fit = node_id not in c.cpu_pins_over
            replica_plans.append(
                ReplicaPlan(
                    seq=seq,
                    node_ids=[node_id],
                    arch="cpu",
                    # 无卡故 gpu_uuids 恒空；headroom 是卡口径，不适用
                    units=[UnitPlacement(role=inst.role, gpu_uuids=[], fit=plan_fit, headroom_gb=None)],
                    fit=plan_fit,
                    issues=[] if plan_fit else ["该节点内存装不下指定的副本份数（可减少副本或强制部署）"],
                )
            )
    c.recommended_placement = RecommendedPlacement(
        gpu_uuids=[],
        node_ids=node_ids,
        arch="cpu",
        total_gpus=0,
        replica_plans=replica_plans,
    )


def _worst_margin(by_uuid: dict, uuids: list, need: float | None) -> tuple:
    """逐卡余量预检（available − 每卡所需；required 缺省只判可配）。返回 (fit, headroom_gb)。

    fit 判据调 :func:`card_reserve` + :func:`reserve_fits`（与 create 的选卡同一条
    双闸：need ≤ 占比预留 且 实况可用 ≥ 预留）——手写 `need ≤ 0.9×total` 会漏掉
    物理侧（被平台外进程占掉一成的卡：need 装得下占比、实况装不下预留），同响应
    里 issues 阻断与 replica_plans 绿灯自相矛盾。余量展示仍按物理口径。
    异构副本按份口径（调用方传本份的 need）；卡不在映射里（不该发生）不参与判定。
    """
    if need is None or not uuids:
        return True, None
    margins = []
    for u in uuids:
        cg = by_uuid.get(u)
        if cg is None:
            continue
        reserve = card_reserve(cg, None)  # 平台默认口径，inspect 判定不看用户显存占比
        if not reserve_fits(reserve, need, cg.available_vram_gb):
            return False, None
        margins.append(cg.available_vram_gb - need)
    if not margins:
        return True, None
    worst = min(margins)
    return worst >= -1e-6, (round(worst, 2) if worst >= -1e-6 else None)


def _pair_manual_plans(cluster_gpus, sel_uuids, topo, per_copy_units, per_copy_need, per_gpu_need, by_uuid) -> list:
    """手选路径：pair_manual_replicas 分组配对 → 逐份 ReplicaPlan。

    配不齐（结构校验类异常）返回空列表——原因已在 _validate_selection 的 issues 里，
    这里不重复报。
    """
    try:
        # 份间可异构：arch 不预过滤（nvidia 份 + cann 份混选各归各桶）。用手选全集
        # cluster_gpus（含被占卡）——一卡多部署只能用户手选，free_pool 会滤掉被占卡导致
        # 配不齐（静默少给副本）或抛错被吞（replica_plans 为空）
        pairs = pair_manual_replicas(
            cluster_gpus,
            sel_uuids,
            per_copy=topo.gpus_per_engine() if topo.is_homogeneous else [u.gpus_per_engine() for u in per_copy_units],
        )
        plans = []
        for seq, (pair_arch, uuids) in enumerate(pairs):
            need = per_copy_need[seq] if seq < len(per_copy_need) else per_gpu_need
            fit, headroom = _worst_margin(by_uuid, uuids, need)
            node_ids = sorted({by_uuid[u].node_id for u in uuids})
            plans.append(
                ReplicaPlan(
                    seq=seq,
                    node_ids=node_ids,
                    arch=pair_arch,
                    units=[UnitPlacement(role="full", gpu_uuids=sorted(uuids), fit=fit, headroom_gb=headroom)],
                    fit=fit,
                    issues=[] if fit else ["所选卡余量不足（每卡需 {:.1f}GB）".format(need or 0)],
                )
            )
        return plans
    except LLMPodValidationError:
        return []  # 配不齐的原因已在上方校验 issues 里


def _run_selection_kernel(c, rec_instances, free_pool, req_vram, per_replica_vram, prefs, by_uuid) -> tuple:
    """自动路径内核：recommend_plan 一次跑（arch_order 逐引擎回落），返回顶层调度字段。

    一次跑内核：同架构够用时不会跨架构，不够才把副本摊到后面的架构（异构副本）。
    此前逐 arch 试整份、要求全份同架构，会把「2 nvidia + 1 Ascend 能放下 3 份」的
    集群误判为放不下。返回 (rec_plan, rec_uuids, sel_nodes, rec_arch)。
    """
    rec_plan = recommend_plan(
        rec_instances,
        free_pool,
        arch=c.arch,
        required_vram_gb=req_vram,
        per_replica_vram=per_replica_vram,
        prefs=prefs,
    )
    rec_uuids: list = []
    sel_nodes: list = []
    rec_arch = ""
    if rec_plan:
        sel_nodes = list(dict.fromkeys(node_id for _inst, node_id, _cards in rec_plan))
        rec_uuids = [getattr(g, "uuid", "") for _i, _n, cards in rec_plan for g in cards]
        # 部署级 arch 回显取首个调度到的卡的实际架构（异构时逐份架构见 replica_plans）
        rec_arch = by_uuid[rec_uuids[0]].arch if rec_uuids and rec_uuids[0] in by_uuid else c.arch
    return rec_plan, rec_uuids, sel_nodes, rec_arch


def _auto_replica_plans(rec_plan, by_uuid, per_copy_need, per_gpu_need, rec_arch) -> list:
    """自动路径逐份重排：plan 逐实例调度按 replica_idx 分组（无新调度调用）。

    InstanceSpec 无引擎身份时（旧形态）回退整体一份。余量按**本份**的每卡需求算
    （异构各份并行度不同），缺省回落同形口径。
    """
    by_replica: dict = {}
    for inst, node_id, cards in rec_plan:
        by_replica.setdefault(inst.replica_idx, []).append((node_id, [getattr(g, "uuid", "") for g in cards]))
    plans = []
    for seq in sorted(by_replica):
        entries = by_replica[seq]
        uuids_flat = [u for _, us in entries for u in us]
        need = per_copy_need[seq] if seq < len(per_copy_need) else per_gpu_need
        fit, headroom = _worst_margin(by_uuid, uuids_flat, need)
        # 份内同 arch（内核单 arch Filter 保证）→ 取本份首卡架构，异构份各自成条
        seq_arch = next((by_uuid[u].arch for u in uuids_flat if u in by_uuid), rec_arch)
        plans.append(
            ReplicaPlan(
                seq=seq,
                node_ids=list(dict.fromkeys(n for n, _ in entries)),
                arch=seq_arch,
                units=[
                    UnitPlacement(role="full", gpu_uuids=sorted(us), fit=fit, headroom_gb=headroom)
                    for _n, us in entries
                ],
                fit=fit,
                issues=[],
            )
        )
    return plans


def _probe_structural(c, rec_instances, rec_plan) -> bool:
    """结构性探测（真实内核干跑，不查显存）：这套拓扑在这个集群按这些架构摆不摆得下。

    推荐调度非空 = 内核已经给出可行解，就不再做结构性否决——两次贪心选卡的约束不同
    （一次带容量过滤、一次不带），可能一边成功一边失败，以「有调度结果」为准，避免同一
    响应里既说不可部署又给出调度结果。返回 structural_failed（探测已报原因的标记）。

    arch_candidates 为空时跳过探测——此时探测只会以 arch_gate 失败并置
    structural_failed，把下方「没有可用于本引擎/本拓扑的架构」的精确分流短路掉。
    """
    # 显式纯 CPU 请求（preference=cpu_only）跳过 GPU 侧探测——CPU 测算解不出时
    # c.cpu_only 恒 False（memory 为 None），不按请求判就会给纯 CPU 请求叠 GPU 侧建议
    cpu_only_intent = c.cpu_only or c.request.offload_preference == resource.SCENARIO_CPU_ONLY
    if not (not c.manual and not cpu_only_intent and not rec_plan and c.arch_candidates):
        return False
    ok, reason = probe_placement(
        rec_instances,
        c.cluster_gpus,
        arch=c.arch,
        arch_order=tuple(c.arch_candidates),
        gpu_selector=c.request.gpu_selector,
    )
    if not ok:
        c.fail(reason or "集群可用卡不足")
        return True
    return False


def _attribute_empty_placement(c, free_pool, rec_uuids, req_vram, structural_failed) -> None:
    """有可选卡但当前 tp 选不出 → 放不下的原因分流，避免误导性提示：

    - 目标架构没有可用卡（架构/镜像/在线都对不上）→ 说清是哪类卡；
    - 当前 tp 下单卡预留装不下模型（最好卡可用 < 占比×总量）→ 提高 tp / 换大卡 / 降占比；
    - 预留装得下但卡数不够（replicas/分布凑不出）→ 建议释放卡或缩小并行度，提高 tp 只会更糟；
    - 模型体积未知（required 缺省）→ 无法比显存，只按卡数口径提示。

    手选卡时跳过：调度由选集配对决定，自动推荐凑不齐不代表选集非法。
    纯 CPU 部署也跳过：不占卡，推荐调度为空是预期（CPU 侧可行性见 _solve_lane 判定）。
    显式 CPU 部署请求（cpu_only 偏好或指定节点 + allow_cpu）同样跳过——模型缺元数据时
    memory 为 None，仅靠 is_cpu_only 判不出来，仍会落到 GPU 侧误导提示。
    """
    request, adapter, topo = c.request, c.adapter, c.topo
    # CPU 请求判定读 cpu_lane_requested（唯一定义处 = allow_cpu or cpu_only 偏好）：
    # 只认 allow_cpu 会漏掉「显式 cpu_only 偏好」，给纯 CPU 请求叠 GPU 侧的
    # 「提高 tp」建议——两条互斥解法同时出现，用户不知道听谁的
    cpu_lane_request = (
        not c.manual_gpus
        and request.cpu_lane_requested
        and adapter.supports_cpu_lane()
        and (
            request.offload_preference == resource.SCENARIO_CPU_ONLY
            or bool(request.gpu_selector and request.gpu_selector.node_id)
        )
    )
    if not (
        not rec_uuids
        and free_pool
        and not c.sel_uuids
        and not resource.is_cpu_only(c.memory)
        and not cpu_lane_request
        and not structural_failed  # 结构性已经报过原因，不再叠一条容量口径
    ):
        return
    tp = max(1, int(topo.tp or 1))
    pp = max(1, int(topo.pp or 1))
    replicas = max(1, int(topo.replicas or 1))
    # 「单卡最大可用」只在真正的候选架构（arch_candidates）内取——跨架构取最大值会拿
    # Ascend 的剩余显存论证 nvidia 部署「已够」，与能不能调度无关；候选为空时直说架构不可用
    arch_names = "/".join(c.arch_candidates)
    candidate_archs = set(c.arch_candidates)
    best_cg = max(
        (cg for cg in free_pool if cg.arch in candidate_archs),
        key=lambda cg: cg.available_vram_gb,
        default=None,
    )
    best_reserve = card_reserve(best_cg, None) if best_cg is not None else None
    if not c.arch_candidates:
        c.fail(
            f"集群没有可用于本引擎/本拓扑的架构（`{c.engine}` 支持 "
            f"{adapter.supported_gpu_archs}，请检查镜像 tag 与卡的在线上报）"
        )
    elif best_cg is None:
        c.fail(
            f"集群没有空闲的 {arch_names} 卡（本部署当前 tp={tp}×replicas={replicas}）；"
            "请释放占用卡、确认卡在线，或改用其它集群"
        )
    elif req_vram is not None and best_reserve + 1e-6 < req_vram / unit_parallel(topo):
        # 每卡需求按首份份内并行度均摊（unit_parallel = tp×pp，与显存估算同口径）——
        # 只除 tp 会把 pp>1 的每卡需求报成 pp 倍，且让本分支抢在「卡数不足」分支之前误报
        c.fail(
            f"模型需约 {req_vram / unit_parallel(topo):.1f}GB/卡（当前 tp={tp}×pp={pp}），但按平台默认显存占比 "
            f"{adapter.default_gpu_memory_utilization:.0%} 单卡只能预留 "
            f"{best_reserve:.1f}GB（{arch_names} 最大可用 "
            f"{best_cg.available_vram_gb:.1f}GB）；请提高 tp 或选择更大显存卡"
        )
    elif req_vram is not None:
        c.fail(
            f"当前 tp={tp}×replicas={replicas} 需要更多满足显存的卡：集群空闲的 {arch_names} 卡不足"
            f"（单卡最大可用 {best_cg.available_vram_gb:.1f}GB，够放单份）；请释放占用卡或缩小并行度"
        )
    else:
        c.fail(
            f"当前 tp={tp}×replicas={replicas} 需要更多可用卡：集群空闲的 {arch_names} 卡不足；请释放占用卡或缩小并行度"
        )
