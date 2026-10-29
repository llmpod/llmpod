"""在线扩缩（显式 ``POST /scale``；副本 = 伸缩单位，份内规格不可变）。

扩容：插新副本/引擎/容器行 → 调度+预留（``preused_uuids`` 排除存量卡，全自动调度）
→ 模型 staging → 容器阶段（复用 task 幂等机制推进：PULL → head → workers →
readiness）→ 投影链把副本推到 serving → ``_finalize`` 补端点（幂等，存量端点不动）
→ 全部新份 serving 后份数回写 ``spec.topology.replicas``、phase 回 RUNNING。

失败语义（运行期 vs 初建分界）：新份任一失败 → **只回滚新份**，份数回原值、存量
副本零感知。分界的权威不变量：``spec.topology.replicas`` 只在扩容全胜时回写，
故「``seq ≥ spec 值``的副本 = 本轮新份」。初建期 all-or-nothing 不在此路径。

缩容：LIFO 选最新 seq 份 → 摘端点（proxy 快照轮询自然出候选池，连绑定删）→
任务化停容器（在途请求收尾）→ 释放 VRAM/卡/三层行 → 份数回写 → 重算健康灯。

可靠性：**desired state 先落库、由 controller 单点驱动**。路由受理时只写
``model_deployment.scale_target``（目标份数）+ phase=SCALING 并唤醒控制循环；真正的
推进由 controller 的 :func:`drive_scaling` 完成——每部署同时只有一个驱动任务
（``controller._active``），故并发提交在结构上不可能双扩。崩溃/重启后意图还在库里，
首扫即续跑，**按落库意图**（而非残留行状态）判方向——「新份已全 serving、spec 尚未
回写」的收尾窗口不会被误判成缩容残留。逐级动作全部 ensure 语义（staging/pull/start/
落行），重入无害。
"""

from __future__ import annotations

import asyncio
import logging
import time


from sqlalchemy import select

from llmpod.apps.manager import dispatch
from llmpod.apps.manager.deployment import task_ops
from llmpod.apps.manager.deployment.constants import (
    DeploymentPhase,
    DeploymentStatus,
    DeployStep,
    LogLevel,
    LogStatus,
    ReplicaStatus,
)
from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.orchestrator import (
    _build_topology_rows,
    _stamp_assigned_instances,
    _stamp_engine_arch,
    get_orchestrator,
)
from llmpod.apps.manager.deployment.schemas import DeploymentConfig, InstanceSpec, Topology, set_topology_copies
from llmpod.apps.manager.deployment.scheduler import validate_same_subnet
from llmpod.apps.manager.deployment.scheduler.gpu import GpuScheduler, infer_selection_arch, plan_placement
from llmpod.apps.manager.deployment.scheduler.pipeline import default_filters, run_filters
from llmpod.apps.manager.deployment.scheduler.rank import RankResolver
from llmpod.apps.manager.deployment.scheduler.topology import expand_standard
from llmpod.apps.manager.deployment.placement import ModelStagingGate, mmproj_path_for
from llmpod.apps.manager.deployment.reconciler import (
    _backfill_ready,
    _ensure_start_tasks,
    _latest_by_key,
    _task_rows_by_keys,
    ensure_pull_tasks,
    probe_pull_images,
    resolve_image,
    resolve_image_size_gb,
)
from llmpod.apps.manager.deployment.stop import StopService
from llmpod.apps.manager.deployment.resource_ledger import ram_claims_by_node
from llmpod.apps.manager.deployment.task_ops import pull_resource_key
from llmpod.config.constants import InstanceStatus, TaskStatus
from llmpod.config.settings import settings
from llmpod.core.exceptions import Conflict, LLMPodError, LLMPodValidationError
from llmpod.db import get_db_context
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.model_deployment_instance import ModelDeploymentInstance
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.services import deploy_log_service, deployment_projection, deployment_service
from llmpod.utils.crypto import generate_uuid
from llmpod.utils.docker_naming import deployment_container_name

log = logging.getLogger("llmpod.apps.manager.deployment.scale")

# 缩容停止预算：给在途请求收尾的排空窗口（超时不再等——此时判缩容失败，行保留重试）
_SCALE_DOWN_STOP_TIMEOUT_S = 30.0
# 扩容协程逐份 staging 的轮询间隔（复用部署期 staging 的节奏）
_SCALE_POLL_INTERVAL_S = 5.0
# 扩容容器阶段（PULL/START 到 READY）的总预算：模型已在盘、镜像多已缓存，给健康
# 检查预算的两倍；超时判扩容失败走回滚，防离线节点把 SCALING 永久挂死
_DRIVE_TIMEOUT_MARGIN = 2.0
# 扩容容器阶段的预算地板：START 的 readiness 已改成「容器活着就一直等」，慢启动
# （冷编译/图捕获）可能超过健康检查预算的两倍，扩容不该比新副本自己更早放弃。
# 本预算只防离线节点把 SCALING 永久挂死，不是启动限时。
_DRIVE_TIMEOUT_FLOOR_S = 3600
# 扩容 staging 总预算：对齐 ENSURE_MODEL 看门狗（placement 建任务处同值）——staging
# 等的就是该任务，预算比它短会先于内层任务错误中止；此预算只兜「任务滞留无人置
# 终态把 SCALING 永久锁死（只能重启 manager 解锁）」，不是下载限时
_STAGE_BUDGET_S = 12 * 3600
# 缩容续摘的连续失败上限：意图保留续摘是为了熬过「节点短暂离线」，永久离线不该
# 无限重试（每轮 5s 一条失败日志 + 用户被 SCALING 锁住改不了份数）。超限放弃意图，
# 部署回到 RUNNING（已摘份数保留），用户可改目标份数重试或换节点
SCALE_DOWN_MAX_FAILS = 8
# 续摘退避：失败后到下一轮尝试的间隔（比 controller 5s 扫描慢一档，防日志/重试刷屏）
_SCALE_RETRY_BACKOFF_S = 30.0
# 缩容续摘失败簿记（进程内；controller 单点驱动）。deployment_id → (连续失败数, 下次
# 允许尝试的 monotonic 时刻)。意图了结/放弃/残影收敛时清项；manager 重启清零重计无害
_scale_fail_streak: dict[str, tuple[int, float]] = {}


def cpu_lane_shards(shards) -> bool:
    """这些分片是否属于纯 CPU 部署（有内存 claim 或 arch=cpu）——扩缩部署谓词的唯一定义处。

    只看实例事实，不看 ``config.allow_cpu``：后者只是「允许落 CPU」，模型装得下卡时部署
    仍解出全量上卡（``_resolve_tier`` 优先 GPU 端），那套部署是显存部署，扩缩容必须放行。
    """
    return any((s.spec or {}).get("ram_claim_gb") or (s.spec or {}).get("gpu_arch") == "cpu" for s in (shards or []))


def _reject_cpu_lane_shards(shards, verb: str) -> None:
    """纯 CPU 部署的分片不支持在线扩缩，命中即拒。

    扩容走 GPU 调度口径（无部署形态求解与内存记账），会把新份静默变成显存部署且不记内存
    claim；缩容/预检同样按 GPU 口径算容量，结论无意义。
    """
    if cpu_lane_shards(shards):
        raise LLMPodValidationError(f"CPU 部署暂不支持在线{verb}：请改副本数后重新部署")


async def _reject_cpu_lane(db, deployment_id: str, verb: str) -> list:
    """按部署当前分片判定并拒绝（见 :func:`_reject_cpu_lane_shards`）；返回已读到的分片行供复用。"""
    shards = await deployment_service.list_instances(db, deployment_id)
    _reject_cpu_lane_shards(shards, verb)
    return shards


def _shards_arch(shards) -> str | None:
    """本部署当前实际调度的架构（首个非 CPU 分片的 ``gpu_arch``）；无行/纯 CPU → None。

    只在**固定镜像**时作为 ``must_include`` 生效：镜像已经在该 arch 上跑起来了，退回
    「默认架构」等于拿另一套镜像去别的卡上找位置。未固定镜像的部署不传约束——镜像由
    catalog 按调度到的卡 arch 解析，跨架构扩容（nvidia 份 + cann 份）是允许的形态。
    """
    for s in shards or ():
        arch = (s.spec or {}).get("gpu_arch")
        if arch and arch != "cpu":
            return arch
    return None


async def load_scale_context(
    db, deployment_id: str
) -> tuple[ModelDeployment, Model, DeploymentConfig, list[ModelDeploymentReplica]]:
    """扩缩共用加载：部署 + 模型 + config + 副本行（按 seq 排序）。"""
    dep, model = await deployment_service.load_deployment(db, deployment_id)
    config = DeploymentConfig.model_validate(dep.spec or {})
    replicas = await deployment_projection.replica_rows_of(db, deployment_id)
    replicas.sort(key=lambda r: r.seq)
    return dep, model, config, replicas


async def preview_scale(db, deployment_id: str) -> dict:
    """只读预检：当前份数 / 每份卡数 / 可扩上限 / 空闲卡 / 扩容调度位置（不落库）。

    ``max_feasible_copies``：逐份循环试探——从当前份数起每次 +1 份跑 ``plan_placement``
    （含存量卡排重），直到装不下；调度预览取再扩一份的调度。容量口径与真实调度同源
    （``GpuScheduler.card_fits``：账本可用量 vs 该卡预留上限），故预览给出的份数在
    ``assign`` 阶段不会因别家预留而失败。异构副本不支持伸缩（request_scale 同口径
    拒绝），预检同样拒绝；CPU 部署同样拒绝（扩容走 GPU 调度口径）。
    """
    dep, model, config, replicas = await load_scale_context(db, deployment_id)
    if dep.status != DeploymentStatus.RUNNING:
        raise Conflict(f"部署当前状态 {dep.status}，仅 running 可调整副本")
    current = len(replicas)
    if not replicas:
        raise Conflict("部署无副本行（旧结构数据），请删除重建后再调整副本")
    topo = config.topology
    if not topo.is_homogeneous:
        raise Conflict("异构副本部署暂不支持在线伸缩（各份形状不同），请删除后按新拓扑重建")
    # 纯 CPU 部署与在线扩容不兼容（扩容无部署形态求解与内存记账）：判据是**实例事实**，不是
    # config.allow_cpu——「允许落 CPU」但模型装得下卡的部署仍走显存，扩缩容应当放行
    existing = await _reject_cpu_lane(db, deployment_id, "调整副本")
    if topo.replicas != current:
        # spec 份数与实际行数漂移（扩容中途的瞬态）：以行数为准呈现
        topo = topo.with_updates(replicas=current)

    adapter = get_adapter(dep.engine)
    gpu_pool = await deployment_service.load_online_gpus(db)
    # 生效占比与真实扩容同口径（存量份落盘值优先），预览容量才不会「预览说能扩、
    # 真扩按继承占比判不够」
    util = adapter.effective_memory_utilization(
        config.engine_params, [(s.spec or {}).get("vram_percent") for s in existing]
    )
    vram_per_gpu = adapter.vram_per_gpu(model, topo, engine_params=config.engine_params, kv_cache=config.kv_cache)
    arch = infer_selection_arch(gpu_pool, None, adapter.default_arch)
    # 容量谓词与 assign 同源（账本可用量 ≥ 本卡预留上限，且占比上限装得下模型）
    sched = GpuScheduler(get_orchestrator().ledger)

    def card_ok(g) -> bool:
        return sched.card_fits(g, vram_per_gpu, util)

    # 调度偏好与 _plan 同源：逐引擎架构回落序（适配器声明服务得了 + 有镜像）+ 本地性 +
    # 卡痕迹——预览与真实扩容必须同口径，否则「预览说能扩、真扩落别处」
    arch_candidates = adapter.schedulable_archs(
        topo,
        gpu_pool=gpu_pool,
        engine_version=config.image_tag,
        explicit_image=config.image,
        must_include=_shards_arch(existing),
    )
    prefs = await deployment_service.placement_prefs(db, model_id=model.id, pool=gpu_pool, arch_order=arch_candidates)

    # 存量卡（本部署已占）：占用的卡对扩容不可见
    existing_uuids = {
        a.gpu_uuid
        for a in (
            await db.execute(
                select(DeploymentGpuAssignment).where(DeploymentGpuAssignment.deployment_id == deployment_id)
            )
        ).scalars()
    }

    def _fits(copies: int) -> list | None:
        """总份数到 copies 是否可行（新增 copies-current 份 + 存量卡排重）；返回新份调度或 None。"""
        insts = expand_standard(
            Topology(tp=topo.tp, pp=topo.pp, replicas=copies - current, nnodes=topo.nnodes), adapter=adapter
        )
        insts.sort(key=lambda s: (s.replica_idx, s.node_rank))
        try:
            # 逐 arch 试探：默认 arch 放不下落候选序后面的（异构集群——nvidia 满了
            # 还能落 cann 卡，份内同 arch 由单 arch Filter 保证）
            return plan_placement(
                insts,
                gpu_pool,
                arch=arch,
                card_ok=card_ok,
                preused_uuids=existing_uuids,
                prefs=prefs,
            )
        except LLMPodValidationError:
            return None

    # 可扩上限：从 current+1 试到放不下（上限探测有界：卡池大小封顶）。
    # pool_size 与调度 Filter 链同口径（online + 容量 + arch 逐候选全算——异构集群任一
    # 引擎支持 arch 的可用卡都算可用）再排除本部署已占卡
    free_uuids: set[str] = set()
    for arch_cand in arch_candidates:
        passed, _rejections, _reasons = run_filters(gpu_pool, default_filters(arch_cand, card_ok=card_ok))
        free_uuids.update(g.uuid for cards in passed.values() for g in cards)
    pool_size = len(free_uuids - existing_uuids)
    per_copy = topo.gpus_per_engine()  # 每份总卡数（tp×pp×ep；跨机 = 同值分布在 nnodes 节点）
    hard_cap = current + (pool_size // max(1, per_copy))
    max_feasible = current
    suggested: list | None = None
    for copies in range(current + 1, hard_cap + 1):
        plan = _fits(copies)
        if plan is None:
            break
        max_feasible = copies
        if copies == current + 1:
            suggested = plan

    free_gpus = pool_size
    suggested_placement = None
    if suggested:
        suggested_placement = {
            "gpu_uuids": [g.uuid for _, _, cards in suggested for g in cards],
            "node_ids": list(dict.fromkeys(nid for _, nid, _ in suggested)),
        }
    return {
        "current_copies": current,
        "per_copy_gpus": per_copy,
        "max_feasible_copies": max_feasible,
        "free_gpus": free_gpus,
        "suggested_placement": suggested_placement,
    }


async def drive_scaling(deployment_id: str) -> None:
    """在途扩缩的驱动（controller 对 running+SCALING 的部署调用；进程内唯一执行者）。

    重入互斥由 controller 的 ``_active`` 保证（每部署同时只有一个驱动任务），本函数
    自身不登记——因此可被每轮扫描重复调用：无意图时只复位相位，有意图时 ``_run_scale``
    按行状态幂等续跑。

    方向按**落库意图**（``dep.scale_target``）判定，不看残留行状态——「新份已全部
    serving、spec 尚未回写」的收尾窗口与「缩容删了一半」在行状态上无法区分，只有
    意图能。判据：意图 > spec 值 ⇒ 扩容（spec 只在全胜时回写）；否则行数 > 意图 ⇒
    缩容未完（缩容逐份回写 spec，spec 可能已等于意图而行还在）；行数 == spec == 意图
    ⇒ 已达成。无意图（只剩 phase 残影）→ 收敛回 RUNNING。
    """
    async with get_db_context() as db:
        dep, model, config, replicas = await load_scale_context(db, deployment_id)
        if dep is None or dep.status != DeploymentStatus.RUNNING:
            # 部署中途离开 RUNNING（stop/delete/外部失败）：进程内失败簿记一并清——
            # 常规 pop 路径（意图了结/放弃/新受理）都不会再走到，残留跨请求泄漏。
            # 落库意图与 phase 残影同样要清：stop/delete/fail 主路径未必走到（如
            # fail 未派发停止的窗口），残留 SCALING 会让 controller 每轮按 phase 捞起
            # 空转、UI 恒显扩缩中。phase 复位到 STOPPING（终态部署唯一允许的子态）
            _scale_fail_streak.pop(deployment_id, None)
            if dep is not None and (dep.scale_target is not None or dep.phase == DeploymentPhase.SCALING):
                dep.scale_target = None
                await deployment_service.set_phase(db, deployment_id, DeploymentPhase.STOPPING, trigger="scale")
            return
        target = dep.scale_target
        spec_n = config.topology.replicas
        rows_n = len(replicas)
    if target is None:
        # 只剩 phase 残影：收敛回 RUNNING（续摘失败簿记一并清——意图没了，簿记无意义）
        _scale_fail_streak.pop(deployment_id, None)
        async with get_db_context() as db:
            await deployment_service.set_phase(db, deployment_id, DeploymentPhase.RUNNING)
        return
    if target > spec_n or rows_n > target:
        # 扩容中断（含「新份已全 serving、spec 未回写」的收尾窗口）：目标 = 落库意图。
        # 行数可能已等于 target——_scale_up 按缺失 seq 展开，此时只补收尾。
        # 缩容中断：缩容逐份即时回写 spec，故 spec 可能已等于意图而行还没删完——以
        # 「还有行要摘」判方向，继续摘除到意图份数。
        streak = _scale_fail_streak.get(deployment_id)
        if streak is not None:
            fails, next_at = streak
            if fails >= SCALE_DOWN_MAX_FAILS:
                # 续摘放弃：清意图 + 相位回 RUNNING（已摘份数保留）——继续重试只会
                # 每 5s 刷一条失败日志，且 SCALING 相位把用户锁在门外（改份数/重试
                # 全被 Conflict 拒）。放弃后用户可改目标份数重新发起
                _scale_fail_streak.pop(deployment_id, None)
                async with get_db_context() as db:
                    dep_row = await db.get(ModelDeployment, deployment_id)
                    if dep_row is not None:
                        dep_row.scale_target = None
                    if dep_row is None or dep_row.status == DeploymentStatus.RUNNING:
                        await deployment_service.set_phase(db, deployment_id, DeploymentPhase.RUNNING)
                await deploy_log_service.record(
                    deployment_id=deployment_id,
                    step=DeployStep.SCALE,
                    level=LogLevel.ERROR,
                    status=LogStatus.FAILED,
                    message=(
                        f"缩容连续 {fails} 次未完成（目标 {target} 份），已停止自动重试；"
                        "请检查节点状态后重新发起副本调整"
                    ),
                    extra={"abandoned": True, "target": target},
                )
                log.warning("scale %s: 缩容续摘连续失败 %d 次，放弃意图", deployment_id[:8], fails)
                return
            if time.monotonic() < next_at:
                return  # 退避窗内：本轮跳过，controller 下轮扫描再来
        await _run_scale(deployment_id, target=target, current=rows_n, spec_n=spec_n, trigger="controller")
    else:
        # 意图已达成（行数 == spec == 意图）：清意图 + 复位
        _scale_fail_streak.pop(deployment_id, None)
        async with get_db_context() as db:
            dep_row = await db.get(ModelDeployment, deployment_id)
            if dep_row is not None:
                dep_row.scale_target = None
            await deployment_service.set_phase(db, deployment_id, DeploymentPhase.RUNNING)


async def request_scale(db, deployment_id: str, copies: int, *, trigger: str = "system") -> None:
    """受理扩缩：守卫 + desired state 落库（意图 + phase=SCALING + 日志），不起协程。

    唯一驱动者是 controller（``phase=SCALING`` 即被 ``_scan_once`` 捞起驱动），本函数
    只写意图——进程内不再有第二个执行者，并发受理因此不可能双扩。调用方须在 ``commit()``
    **之后** ``get_controller().wake()``（提交前唤醒会让 controller 读到旧相位空转一轮）。

    目标与**实际行数**相同即无操作（短路返回：不写 SCALING、不记日志）：缩容失败会留下
    「行数 > spec」的漂移，拿 spec 比对会把用户重试同一目标误判成无变化。
    """
    dep, model, config, replicas = await load_scale_context(db, deployment_id)
    if dep.status != DeploymentStatus.RUNNING:
        raise Conflict(f"部署当前状态 {dep.status}，仅 running 可调整副本")
    if dep.phase == DeploymentPhase.SCALING:
        raise Conflict("副本调整进行中，请等待完成")
    current = len(replicas)
    if not replicas:
        raise Conflict("部署无副本行（旧结构数据），请删除重建后再调整副本")
    # 异构副本无「单一形状」可复制——扩缩的新份拷贝哪份的模板无从回答（逐份伸缩
    # = 形状寻址的增删语义，另立设计），v1 拒绝；同形部署照旧伸缩
    if not config.topology.is_homogeneous:
        raise Conflict("异构副本部署暂不支持在线伸缩（各份形状不同），请删除后按新拓扑重建")
    # 纯 CPU 部署与在线扩缩不兼容（扩缩走 GPU 调度口径，无部署形态求解与内存记账）。
    # 判据是**实例事实**（与 preview、_scale_up 同一谓词），不看 config.allow_cpu
    await _reject_cpu_lane(db, deployment_id, "调整副本")
    if copies == current:
        return  # 已是目标态：无操作
    # 新意图覆盖旧簿记（可能是上次放弃后的重试）
    _scale_fail_streak.pop(deployment_id, None)
    dep.scale_target = copies
    await deployment_service.set_phase(db, deployment_id, DeploymentPhase.SCALING, trigger=trigger)
    await deploy_log_service.append_log(
        db,
        deployment_id=deployment_id,
        step=DeployStep.SCALE,
        level=LogLevel.INFO,
        status=LogStatus.RUNNING,
        message=f"在线调整副本：{current} → {copies} 份",
        extra={"trigger": trigger, "from": current, "to": copies},
    )
    await db.flush()


async def _run_scale(deployment_id: str, *, target: int, current: int, spec_n: int, trigger: str) -> None:
    """扩缩共享外壳：按落库意图分流 + 失败收敛 + 完成复位。

    意图与 phase=SCALING 由 :func:`request_scale`（受理侧）写好，本函数**只读**——
    写意图的只有受理侧一处，驱动的只有 controller 一个。
    ``current`` / ``spec_n`` 由调用方给出（它刚为判方向加载过上下文）：本函数不再二次
    加载 dep+model+副本行+spec 校验，只在自己的写点按需取行。

    **CancelledError 不吞**：manager 重启等取消场景保留 SCALING + 意图给 drive_scaling
    续跑（不复位 phase——那是「扩缩已结束」的标记，取消时是假的）。
    """
    # 方向按意图与 spec 的关系定：spec 只在扩容全胜时回写，故 target > spec = 扩容
    scale_up = target > spec_n
    canceled = False
    intent_resolved = False
    try:
        if scale_up:
            await _scale_up(deployment_id, target, trigger)
        else:
            await _scale_down(deployment_id, current, target, trigger)
        intent_resolved = True
        _scale_fail_streak.pop(deployment_id, None)
    except asyncio.CancelledError:
        # 取消不等于失败：半成品行 + SCALING + 意图留给 drive_scaling 续跑
        canceled = True
        log.warning("scale %s 被取消（保留 SCALING 供续跑）", deployment_id[:8])
        raise
    except Exception as e:
        # 失败收敛：扩容失败回滚新份 + 份数回原值；缩容失败（停不下）保留行——意图
        # 是否了结由 _handle_scale_failure 的返回值决定（见下方注释）
        intent_resolved = await _handle_scale_failure(deployment_id, current, target, e, rollback_new=scale_up)
        if intent_resolved:
            _scale_fail_streak.pop(deployment_id, None)
        else:
            # 缩容未了结：连续失败计数 +1，供 drive_scaling 的退避/放弃门用
            fails, _ = _scale_fail_streak.get(deployment_id, (0, 0.0))
            _scale_fail_streak[deployment_id] = (fails + 1, time.monotonic() + _SCALE_RETRY_BACKOFF_S)
        raise
    finally:
        # 完成/失败（回滚已完成）才清意图 + 复位；取消穿出不复位
        if not canceled:
            async with get_db_context() as db:
                dep = await db.get(ModelDeployment, deployment_id)
                if dep is not None and intent_resolved:
                    # 意图清理按收敛结果分叉：扩容失败已整份回滚、缩容成功才到这——
                    # 留着意图只会让下一轮把刚回滚的动作再做一遍，清。缩容失败
                    # （intent_resolved=False）是「摘到一半停不下」：行数仍 > 意图，意图与
                    # SCALING 相位都得保留才有下一轮 drive_scaling 续摘——清了就静默
                    # 卡在中间份数（用户要的份数无人再兑现，controller 不再捞起）。
                    # 相位同理：仅在收敛后且仍 RUNNING 时复位（stop/delete 竞态下别把
                    # STOPPING 盖回 RUNNING；缩容失败保持 SCALING 供续跑）
                    dep.scale_target = None
                    if dep.status == DeploymentStatus.RUNNING:
                        await deployment_service.set_phase(db, deployment_id, DeploymentPhase.RUNNING)


def _assert_running(dep) -> None:
    """扩缩轮询的存活守卫：部署离开 RUNNING（stop/delete）即抛错中止本轮。

    没有它，在途扩容会继续往已停止的部署上拉新容器——stop 只取消**已存在**的 START
    task，中止后新下发的照旧执行。抛错走 ``_run_scale`` 的失败收敛：扩容回滚新份、
    缩容保留行（已摘的端点不复活）。

    传入**已加载的部署行**：轮询点基本都已取过它（``load_scale_context``），再自己开
    session 查一遍既是重复 I/O，也可能读到与本次判定不同刻的行。
    """
    if dep is None:
        raise LLMPodError("部署已删除，中止扩缩")
    if dep.status != DeploymentStatus.RUNNING:
        raise LLMPodError(f"部署已离开运行态（{dep.status}），中止扩缩")


async def _scale_up(deployment_id: str, target: int, trigger: str) -> None:
    """扩到 target 份：补展开缺失份 + 全自动调度 + staging + 交容器阶段。

    容器阶段不走 reconciler 的部署 phase（那是 creating 期的单线性图）——本协程
    直接用 task_ops 幂等原语逐份驱动：PULL（共享池天然去重）→ START 全分片 →
    等 READY（双门：实例状态或 task SUCCESS，同 _wait_rows 语义）。

    **续跑分界按绝对份序号**：``seq ∈ [0, target)`` 中 DB 里还没有的份才展开/调度/落行
    （上一轮已落库的份整份跳过——它们有容器行、卡已预留，重复展开会撞唯一键并把卡
    再占一遍）。份数回写前的权威不变量「seq ≥ spec.replicas = 本轮新份」不受影响。

    纯 CPU 部署暂不支持在线扩容：扩容走 GPU 调度口径（无 RAM 记账），
    会把新份静默变成显存部署且不记内存 claim。明确拒绝，让用户改副本数重新部署。
    """
    orch = get_orchestrator()
    async with get_db_context() as db:
        dep, model, config, replicas = await load_scale_context(db, deployment_id)
        topo = config.topology
        adapter = get_adapter(dep.engine)

        # 1. 带内存预留的部署不进扩容（扩容走 GPU 调度口径，无部署形态求解与 RAM 记账）：
        # 纯 CPU 部署分片带 ram_claim_gb + cpu arch（与 preview/受理同一谓词）
        existing_shards = await deployment_service.list_instances(db, deployment_id)
        _reject_cpu_lane_shards(existing_shards, "扩容")

        # 2. 补展开缺失份（绝对 seq）：同形副本各份形状一致，展开产物按序映射到缺失序号
        present_seqs = {r.seq for r in replicas}
        missing_seqs = sorted(set(range(target)) - present_seqs)
        insts: list[InstanceSpec] = []
        if not missing_seqs:
            # 上一轮已把份展开完（续跑收尾）：跳过调度与落行，直接进容器阶段
            log.info("scale %s: 份已全部展开（target=%d），直接进入容器阶段收尾", deployment_id[:8], target)
        else:
            insts = expand_standard(
                Topology(tp=topo.tp, pp=topo.pp, replicas=len(missing_seqs), nnodes=topo.nnodes), adapter=adapter
            )
            for inst in insts:
                inst.instance_id = generate_uuid()
                # 展开产物的 replica_idx 是 0..n-1 的相对序，映射回绝对份序号
                inst.replica_idx = missing_seqs[inst.replica_idx]
            base_rank = max((s.rank for s in existing_shards), default=-1) + 1
            for idx, inst in enumerate(insts):
                inst.rank = base_rank + idx  # 全局序号接在存量之后（不撞已有 rank）
        replica_rows, engine_rows = _build_topology_rows(deployment_id, insts)

        if insts:
            # 3. 调度 + 预留（全自动调度；存量卡排除）
            gpu_pool = await deployment_service.load_online_gpus(db)
            vram_per_gpu = adapter.vram_per_gpu(
                model, topo, engine_params=config.engine_params, kv_cache=config.kv_cache
            )
            # 生效占比：存量份 spec 落盘的 vram_percent 优先于 config 引擎 flag（口径见
            # effective_memory_utilization）——新份与存量份必须同占比，否则引擎实际行为
            # 与账本记账两头分叉
            util = adapter.effective_memory_utilization(
                config.engine_params, [(s.spec or {}).get("vram_percent") for s in existing_shards]
            )
            arch = infer_selection_arch(gpu_pool, None, adapter.default_arch)
            # 与 _plan 同三件套：架构回落序（服务得了 + 有镜像）+ 本地性 + 卡痕迹
            # must_include：本部署当前架构——仅固定镜像时收窄到该 arch；未固定时镜像按
            # 调度到的卡 arch 由 catalog 解析，跨架构扩容是允许的
            arch_candidates = adapter.schedulable_archs(
                topo,
                gpu_pool=gpu_pool,
                engine_version=config.image_tag,
                explicit_image=config.image,
                must_include=_shards_arch(existing_shards),
            )
            prefs = await deployment_service.placement_prefs(
                db, model_id=model.id, pool=gpu_pool, arch_order=arch_candidates
            )
            assignments = await GpuScheduler(orch.ledger).assign(
                deployment_id,
                insts,
                gpu_pool,
                vram_per_gpu,
                arch,
                vram_percent=util,
                # 扩容全自动调度：逐 arch 试探（默认 arch 放不下落候选序后面的——
                # nvidia 满了还能落 cann 卡，份内同 arch 由单 arch Filter 保证）
                prefs=prefs,
                preused_uuids={
                    a.gpu_uuid
                    for a in (
                        await db.execute(
                            select(DeploymentGpuAssignment).where(
                                DeploymentGpuAssignment.deployment_id == deployment_id
                            )
                        )
                    ).scalars()
                },
            )

            # 4. rank 解析 + 端口（同 _plan 口径：逐引擎 rendezvous、逐实例服务口）
            node_ids = {i.assigned_node for i in insts if i.assigned_node}
            node_ips = await deployment_service.load_node_ips(db, node_ids)
            if not config.force and any(max(1, int(i.nnodes or 1)) > 1 for i in insts):
                validate_same_subnet(node_ips, settings.multinode_same_subnet_prefix)
            RankResolver.fill(
                insts,
                default_master_port=adapter.default_master_port,
                get_node_ip=lambda nid: node_ips.get(nid, "127.0.0.1"),
            )
            _local_taken: dict[tuple[str | None, str], set[int]] = {}
            await deployment_service.assign_rendezvous_ports(db, insts, _local_taken)
            # 逐实例字段收口（arch/目标卡型号/驱动按实际调度到的卡推导——异构集群扩容份
            # 可能落在不同 arch），与 _plan 同款
            await _stamp_assigned_instances(
                db,
                insts,
                adapter=adapter,
                model=model,
                config=config,
                gpu_pool=gpu_pool,
                arch=arch,
                util=util,
                local_taken=_local_taken,
            )

            # 5. 落行（replica/engine 先于容器行）。引擎行 arch 随 lead 分片实际调度到的卡
            _stamp_engine_arch(engine_rows, insts, arch)
            await deployment_service.persist_topology(db, replica_rows, engine_rows)
            await deployment_service.persist_instances(db, deployment_id, insts)
            await deployment_service.persist_assignments(db, assignments, deployment_id, vram_per_gpu, arch)
            await db.flush()

    # 6. staging（新份的目标节点须模型在盘；已在盘则秒过）
    await _stage_new_replicas(deployment_id)

    # 7. 容器阶段：PULL → START → 等 READY（逐轮推进，双门判定）
    await _drive_new_replicas(deployment_id)

    # 8. 全部新份 serving → 份数回写 + finalize 补端点（幂等；存量端点不动）
    async with get_db_context() as db:
        dep, model, config, replicas = await load_scale_context(db, deployment_id)
        # 收尾窗口守卫：drive 的最终 READY 判定之后到此处，stop/delete 仍可能落地
        # （存量端点已被摘、orchestration task 已取消）——不再复活端点/回写份数
        _assert_running(dep)
        base = config.topology.replicas
        all_new = [r for r in replicas if r.seq >= base]
        if any(r.status != ReplicaStatus.SERVING for r in all_new):
            raise LLMPodError(f"扩容未全部 serving（{'/'.join(r.status for r in all_new)}）")
        irows = await deployment_service.list_instances(db, deployment_id)
        irows.sort(key=lambda r: r.rank)
        specs = [InstanceSpec.model_validate(r.spec or {}) for r in irows]
        new_ids = {
            e.instance_id for e in await deployment_projection.engine_rows_of(db, [r.replica_id for r in all_new])
        }
        achieved = [r.instance_id for r in irows if r.model_deployment_instance_id in new_ids]
        # 只认领新份（存量副本可能正 down/自愈中，盲盖会把投影链的真实状态抹掉）
        await orch._finalize(db, dep, specs, get_adapter(dep.engine), model, achieved_ids=achieved)
        spec = dict(dep.spec or {})
        spec["topology"] = set_topology_copies(spec.get("topology") or {}, len(replicas))
        dep.spec = spec
        await deploy_log_service.append_log(
            db,
            deployment_id=deployment_id,
            step=DeployStep.SCALE,
            level=LogLevel.INFO,
            status=LogStatus.SUCCESS,
            message=f"扩容完成：{len(replicas)} 份全部服务中",
            extra={"trigger": trigger, "copies": len(replicas)},
        )
        await db.flush()


async def _stage_new_replicas(deployment_id: str) -> None:
    """新份目标节点声明式模型分发（幂等；已在盘秒过）。

    终态失败即抛（下载超自动重试上限且无新任务可建——与部署期 staging 同判定），
    不无限等：永久失败把 SCALING 挂死没有意义，交由上层回滚新份。
    """
    # 复核任务跨轮保留：同一 node 只派发一次（每轮重派会不断新建任务、永远等不到
    # 同一任务的终态，与 node_model 缓存来回打架）；终态失败才清出，由
    # ensure_present 建下一 attempt。
    # 总预算 = ENSURE_MODEL 看门狗同款 12h：下载可合法跑数小时（体积估算如 2GB/min
    # 会先于内层任务错误中止它），预算只兜「任务滞留无人置终态把 SCALING 锁死」
    gate: ModelStagingGate | None = None
    deadline = asyncio.get_running_loop().time() + _STAGE_BUDGET_S
    while True:
        if asyncio.get_running_loop().time() > deadline:
            raise LLMPodError(
                f"新副本模型 staging 超预算（{_STAGE_BUDGET_S // 60} 分钟，疑似节点离线/下载卡死），中止扩容"
            )
        async with get_db_context() as db:
            dep, model, config, replicas = await load_scale_context(db, deployment_id)
            _assert_running(dep)
            base = config.topology.replicas
            new_reps = [r for r in replicas if r.seq >= base]
            if not new_reps:
                return
            engines = await deployment_projection.engine_rows_of(db, [r.replica_id for r in new_reps])
            shards = await deployment_service.list_instances(db, deployment_id)
            new_engine_ids = {e.instance_id for e in engines}
            pending_nodes = {
                s.assigned_node for s in shards if s.model_deployment_instance_id in new_engine_ids and s.assigned_node
            }
            if not pending_nodes:
                return

        if gate is None:
            gate = ModelStagingGate(model.id)
        over_cap = await gate.dispatch(pending_nodes)
        async with get_db_context() as db:
            confirmed, by_node = await gate.advance(db, pending_nodes, over_cap)
            if pending_nodes <= confirmed:
                # 模型路径写回新份容器 spec（同事务内重取行，避免 detached 后写不落库）。
                # 与初建路径同口径：只认**节点回报**的权威路径（None 不覆盖预估，否则会写出
                # model_path=None → 下发 `--model None`），mmproj 按回报目录重算（否则 --mmproj
                # 停在 _plan 的预估路径，容器里打不开投影器）
                shards = await deployment_service.list_instances(db, deployment_id)
                for s in shards:
                    if s.model_deployment_instance_id not in new_engine_ids:
                        continue
                    nm = by_node[s.assigned_node]
                    if not nm.model_path:
                        continue
                    await deployment_service.update_instance_spec(
                        db, s.instance_id, nm.model_path, mmproj_path_for(model, nm.local_dir)
                    )
                return
        await asyncio.sleep(_SCALE_POLL_INTERVAL_S)


async def _drive_new_replicas(deployment_id: str) -> None:
    """新份容器阶段推进（PULL 共享池 → START 全分片 → 双门等 READY → 投影）。

    与 reconciler 的差异：只驱动新份（spec 分界），部署 phase 不动（SCALING 由
    _run_scale 维护）；attempt 超限或总预算超时抛错（scale 失败 → 回滚新份）。
    预算 = 健康检查超时 ×2（不足一小时按一小时）：防离线节点把 SCALING 永久挂死。

    **不走 ``_ensure_start_tasks`` 的重试耗尽分支**：它调 ``fail_deployment`` 会把整个
    部署（含存量健康副本）停掉并标 failed，与「扩容失败只回滚新份」的分界冲突——故
    先自查 attempt 耗尽并就地抛错，让收敛走 :func:`_handle_scale_failure`。
    """
    async with get_db_context() as db:
        dep, model, config, _replicas = await load_scale_context(db, deployment_id)
        # 健康检查预算的两倍打底，再套一层地板（见 _DRIVE_TIMEOUT_FLOOR_S）
        drive_timeout_s = max(config.health_check.timeout_seconds * _DRIVE_TIMEOUT_MARGIN, _DRIVE_TIMEOUT_FLOOR_S)

    deadline = asyncio.get_running_loop().time() + drive_timeout_s

    def _check_deadline():
        if asyncio.get_running_loop().time() > deadline:
            raise LLMPodError(f"新副本容器阶段超预算（{drive_timeout_s:.0f}s，疑似节点离线/启动卡死）")

    # ---- PULL ----
    async with get_db_context() as db:
        dep, model, config, replicas = await load_scale_context(db, deployment_id)
        base = config.topology.replicas
        new_reps = [r for r in replicas if r.seq >= base]
        engines = await deployment_projection.engine_rows_of(db, [r.replica_id for r in new_reps])
        new_engine_ids = {e.instance_id for e in engines}
        shards = [
            s
            for s in await deployment_service.list_instances(db, deployment_id)
            if s.model_deployment_instance_id in new_engine_ids
        ]
        adapter = get_adapter(dep.engine)
        # 节点 → 镜像 / 镜像体积两表同循环产出（spec 只解析一次）；拉取看门狗按
        # 体积分桶用（与 reconciler 同法，未标注体积落 7200s 平顶）
        node_image: dict[str, str] = {}
        node_image_size: dict[str, float | None] = {}
        for r in shards:
            if not r.assigned_node or r.assigned_node in node_image:
                continue
            inst = InstanceSpec.model_validate(r.spec or {})
            node_image[r.assigned_node] = resolve_image(dep.engine, adapter, config, inst)
            node_image_size[r.assigned_node] = resolve_image_size_gb(dep.engine, adapter, config, inst)
    if node_image:
        keys = [pull_resource_key(n, img) for n, img in node_image.items()]
        image_sizes = {node_image[n]: node_image_size.get(n) for n in node_image}
        while True:
            _check_deadline()
            async with get_db_context() as db:
                _assert_running(await db.get(ModelDeployment, deployment_id))
            # 每轮 ensure（与部署期 reconciler 同口径，见 ensure_pull_tasks）：本地缓存
            # 探测命中直接记 SUCCESS、失败行按末尾连续失败数决定重建、PENDING/RUNNING
            # 不打扰；全 SUCCESS 即完成。探测在事务外并发（见 probe_pull_images docstring）
            prev_latest = _latest_by_key(await _task_rows_by_keys(keys))
            probes = await probe_pull_images(node_image, prev_latest)
            async with get_db_context() as db:
                to_dispatch, exhausted = await ensure_pull_tasks(
                    db, node_image, prev_latest, probes, deployment_id=deployment_id, image_sizes=image_sizes
                )
                await db.flush()
            if exhausted is not None:
                raise LLMPodError(f"新副本镜像拉取失败（重试耗尽）: {exhausted[1]}")
            for t in to_dispatch:
                await task_ops.dispatch_task(t)
            # 重新拉取终态行判完成（本轮 ensure 可能补记了 SUCCESS 探测行/建了新 PENDING 行）
            latest = _latest_by_key(await _task_rows_by_keys(keys))
            if all((t := latest.get(k)) is not None and t.status == TaskStatus.SUCCESS for k in keys):
                break
            await asyncio.sleep(_SCALE_POLL_INTERVAL_S)

    # ---- START（全分片并发，head-first 门控交给自愈/重试语义：START task 幂等）----
    # 新份引擎身份循环前固定：seq ≥ spec 值的行集在成功路径上只增不减（失败即抛错离开），
    # 每轮重查 replica/engine 行是纯重复——容器行仍逐轮取（状态在变）
    async with get_db_context() as db:
        _dep, _model, config, replicas = await load_scale_context(db, deployment_id)
        new_reps = [r for r in replicas if r.seq >= config.topology.replicas]
        if not new_reps:
            return
        engines = await deployment_projection.engine_rows_of(db, [r.replica_id for r in new_reps])
        new_engine_ids = {e.instance_id for e in engines}
    while True:
        _check_deadline()
        async with get_db_context() as db:
            shards = [
                s
                for s in await deployment_service.list_instances(db, deployment_id)
                if s.model_deployment_instance_id in new_engine_ids
            ]
            dep, model, config, _reps = await load_scale_context(db, deployment_id)
            _assert_running(dep)
            adapter = get_adapter(dep.engine)
            # 重试耗尽由 _ensure_start_tasks 就地抛错（fail_on_exhausted=False）：省掉这里
            # 先自查一遍 START task 行的重复查询，且与它内部那次查询同源同刻
            latest = await _ensure_start_tasks(dep, model, config, adapter, shards, fail_on_exhausted=False)
            if latest is None:
                raise LLMPodError("新副本启动失败（重试耗尽）")
            # 双门兜底：START task SUCCESS 但 READY 上报丢失（隧道离线即丢弃不重试）→
            # 补写 READY，否则健康的份会被超时回滚（首建路径由 _backfill_ready 救回，
            # 扩容期 controller 不跑运行期自愈，此处必须自己补）。复用刚取到的 task 映射
            await _backfill_ready(deployment_id, shards, latest)
        await asyncio.sleep(_SCALE_POLL_INTERVAL_S)
        async with get_db_context() as db:
            shards = [
                s
                for s in await deployment_service.list_instances(db, deployment_id)
                if s.model_deployment_instance_id in new_engine_ids
            ]
            if all(s.status == InstanceStatus.READY for s in shards):
                await deployment_projection.project_upwards(db, deployment_id)
                return
            if any(s.status == InstanceStatus.FAILED for s in shards):
                raise LLMPodError("新副本启动失败（实例 FAILED）")


async def _scale_down(deployment_id: str, current: int, target: int, trigger: str) -> None:
    """缩 current → target：LIFO 摘最新份——摘端点 → 停容器（确认停止）→ 释放 → 删三层行。

    停止未确认（预算耗尽仍非 STOPPED）即抛错中止：VRAM/行不释放——「确认停止才
    释放」与失败收敛同一不变量，强放会让回归节点上的旧容器与新副本抢同一张卡。
    """
    remove_seqs = list(range(target, current))  # LIFO：seq 大的先处理（倒序遍历）
    for seq in reversed(remove_seqs):
        async with get_db_context() as db:
            dep, _model, _config, replicas = await load_scale_context(db, deployment_id)
            _assert_running(dep)
            rep = next((r for r in replicas if r.seq == seq), None)
            if rep is None:
                continue  # 已删（幂等重入）
            engines = await deployment_projection.engine_rows_of(db, [rep.replica_id])
            engine_ids = {e.instance_id for e in engines}
            shards = [
                s
                for s in await deployment_service.list_instances(db, deployment_id)
                if s.model_deployment_instance_id in engine_ids
            ]
            # 1. 摘端点（连绑定；proxy 快照轮询自动出流量池）
            endpoints = list(
                (
                    await db.execute(
                        select(ModelEndpoint).where(ModelEndpoint.model_deployment_replica_id == rep.replica_id)
                    )
                ).scalars()
            )
            await deployment_service.delete_endpoints_with_bindings(db, endpoints)
            await db.flush()

        # 2. 停容器（排空窗口 = 停止预算，逐阶段计）；未确认停止即中止本轮缩容（行保留，重试续跑）
        stopped = await _stop_replica_shards(deployment_id, shards)
        if not stopped:
            raise LLMPodError(
                f"副本 r{seq + 1} 容器未在停止预算（{_SCALE_DOWN_STOP_TIMEOUT_S:.0f}s/阶段）内确认停止"
                "（节点离线/停止超时），已保留本份，将自动重试剩余调整"
            )

        # 3. 释放 VRAM（进程内 + assignment 行）+ 删三层行 + 份数回写 + 端点改名 + 健康灯重算。
        #    spec 写回用本 session 重取的 dep 行——step-1 的 dep 来自已关闭 session，
        #    detached 对象上的写不会落库
        async with get_db_context() as db:
            dep_row = await db.get(ModelDeployment, deployment_id)
            if dep_row is None:
                return
            uuids = await _purge_replica_rows(db, deployment_id, engines, shards, [rep])
            # 份数回写：删完这份后的实际剩余行数
            remaining_replicas = await deployment_projection.replica_rows_of(db, deployment_id)
            remaining = len(remaining_replicas)
            spec = dict(dep_row.spec or {})
            spec["topology"] = set_topology_copies(spec.get("topology") or {}, remaining)
            dep_row.spec = spec
            # 副本集变了 → 端点展示名重算（2→1 份时幸存者退回裸名）；复用刚查到的副本行
            await deployment_projection.sync_endpoint_names(db, dep_row, replicas=remaining_replicas)
            await deploy_log_service.append_log(
                db,
                deployment_id=deployment_id,
                step=DeployStep.SCALE,
                level=LogLevel.INFO,
                status=LogStatus.SUCCESS,
                message=f"缩容完成：副本 r{seq + 1} 已移除（剩 {remaining} 份）",
                extra={"trigger": trigger, "seq": seq, "copies": remaining},
            )
            await db.flush()
        # 释放改为差集口径：commit 在 release 之前，本协程若在两步之间被取消
        # （controller 停在途扩缩），行已删、按 uuids 的释放无行可查——ledger 是
        # 进程内账本，这几张卡就成了伴随进程生命周期的幽灵预留（预览/准入少算
        # 卡）。与扩容失败回滚的 leak_uuids 兜底同款：对「账本现有预留 − 存量行
        # 持卡」求差——行已删也无妨，新会话读到的就是删除后状态
        orch = get_orchestrator()
        async with get_db_context() as db:
            keep_uuids = _assigned_uuids(await deployment_service.list_instances(db, deployment_id))
        leak_uuids = set(orch.ledger.get_reserved_for_deployment(deployment_id)) - keep_uuids
        await orch.ledger.release_cards(deployment_id, set(uuids) | leak_uuids)
        await _release_ram_of_shards(deployment_id, shards)
        # 4. 删除被摘份的停止容器：行已删，容器名（含 instance_id）不会再被任何路径引用，
        #    留在节点上只占磁盘（stop 是纯停不删；扩容回滚与 delete 部署都做清理，缩容
        #    此前漏了）。best-effort 并发删，失败交节点磁盘治理兜底
        await asyncio.gather(
            *[
                _remove_stopped_container(s.assigned_node, deployment_container_name(deployment_id, s.instance_id))
                for s in shards
                if s.assigned_node
            ]
        )
        async with get_db_context() as db:
            await deployment_projection.project_upwards(db, deployment_id)


async def _remove_stopped_container(node_id: str, name: str) -> None:
    """best-effort 删除节点上的停止容器（缩容摘份后的磁盘回收）。"""
    try:
        await dispatch.remove_container(node_id, name, force=True)
    except Exception as e:
        log.warning("scale down: 容器 %s@%s 删除失败（best-effort）: %s", name, node_id, e)


async def _release_ram_of_shards(deployment_id: str, shards) -> None:
    """释放被移除分片的内存预留（纯 CPU 实例才有 claim，其余为空 no-op）。"""
    amounts = ram_claims_by_node(shards)
    if amounts:
        await get_orchestrator().ledger.release_ram_nodes(deployment_id, amounts)


def _assigned_uuids(shards) -> set[str]:
    """从实例行集抽取行内 spec 记录的持卡 uuid 集（assignment 对账的口径）。"""
    return {u for s in shards for u in ((s.spec or {}).get("assigned_gpu_uuids") or [])}


async def _purge_replica_rows(
    db, deployment_id: str, engines: list[ModelDeploymentInstance], shards: list[Instance], replicas: list
) -> set[str]:
    """删副本三层行（容器/引擎/副本）+ 对应 GPU assignment 行；返回涉及的 GPU uuid 集。

    缩容摘份与扩容失败回滚共用的破坏性路径——只删「这些份」的行，assignment 按行内
    spec 记录的卡精确对账（不碰存量份的卡）。传入行可能来自已关闭 session（detached
    不能直接 delete），按主键在本 session 重取后删。
    """
    uuids = _assigned_uuids(shards)
    for a in (
        await db.execute(select(DeploymentGpuAssignment).where(DeploymentGpuAssignment.deployment_id == deployment_id))
    ).scalars():
        if a.gpu_uuid in uuids:
            await db.delete(a)
    for s in shards:
        row = await db.get(Instance, s.instance_id)
        if row is not None:
            await db.delete(row)
    for e in engines:
        row = await db.get(ModelDeploymentInstance, e.instance_id)
        if row is not None:
            await db.delete(row)
    for r in replicas:
        row = await db.get(ModelDeploymentReplica, r.replica_id)
        if row is not None:
            await db.delete(row)
    return uuids


async def _stop_replica_shards(deployment_id: str, shards) -> bool:
    """停一个副本的全部容器分片，返回是否在预算内全部确认 STOPPED。

    复用 :class:`StopService`（限定到本份的分片）：取消在途 START task（否则 STOP 与
    START 竞态会让容器在账本已释放的卡上重新起来）→ 置 STOPPING + 下发 STOP → 双门
    等 STOPPED → 补写上报丢失的 STOPPED。缩容与自愈重启的互斥靠这四步，缺一步都会
    留下无人管理的容器。
    """
    return await StopService().stop_instances(deployment_id, instances=list(shards), timeout=_SCALE_DOWN_STOP_TIMEOUT_S)


async def _handle_scale_failure(
    deployment_id: str, original: int, target: int, error: Exception, *, rollback_new: bool
) -> bool:
    """扩缩失败的收敛。返回**意图是否已了结**（True=清意图收口；False=保留意图续跑）。

    扩容失败：只回滚新份（seq ≥ spec 值），份数/存量不动，意图了结。
    缩容失败：不回滚（已摘的端点不复活）且**意图保留**——行数仍 > 意图，下一轮
    ``drive_scaling`` 续摘剩余份（每份摘除幂等，从「哪份还在」起续）。
    **预留释放不依赖残留行**：``assign`` 成功到落行之间抛错时库里没有行，若只按行内
    记录的卡释放就会留下幽灵预留（此后 inspect/预览/准入都少算这些卡）。故按「账本
    里本部署的预留 − 存量份仍持有的卡」求差集释放，任何失败点都覆盖。
    """
    log.warning("scale %s 失败（rollback_new=%s, 回滚到 %d）: %r", deployment_id[:8], rollback_new, original, error)
    if not rollback_new:
        # 缩容路径：不回滚、意图保留（下轮续摘）；落失败日志收口受理时的 RUNNING 条目
        await deploy_log_service.record(
            deployment_id=deployment_id,
            step=DeployStep.SCALE,
            level=LogLevel.WARN,
            status=LogStatus.FAILED,
            message=f"缩容未完成（已摘部分保留）：{getattr(error, 'user_reason', None) or error}，将自动重试剩余份",
            extra={"error": str(error)[:500], "from": original, "to": target},
        )
        return False
    orch = get_orchestrator()
    async with get_db_context() as db:
        dep, model, config, replicas = await load_scale_context(db, deployment_id)
        base = config.topology.replicas
        doomed = [r for r in replicas if r.seq >= base]
        engines = await deployment_projection.engine_rows_of(db, [r.replica_id for r in doomed])
        engine_ids = {e.instance_id for e in engines}
        all_shards = await deployment_service.list_instances(db, deployment_id)
        shards = [s for s in all_shards if s.model_deployment_instance_id in engine_ids]
        # 存量份仍持有的卡（新份不释放这些）：行还在的存量分片 + 无行但账本里属于存量的
        doomed_ids = {s.instance_id for s in shards}
        keep_uuids = _assigned_uuids(s for s in all_shards if s.instance_id not in doomed_ids)
        leak_uuids = set(orch.ledger.get_reserved_for_deployment(deployment_id)) - keep_uuids
        # 残留容器清理线索（行删除后不能再查）：best-effort stop+rm，按容器名直停
        leftovers = [(s.assigned_node, deployment_container_name(deployment_id, s.instance_id)) for s in shards]
        # 人读原因（调度拒绝自带的中文说明）优先呈现，原始异常串另附 extra 供排障——
        # 与创建路径 orchestrator._handle_deploy_failure 同口径，同一个失败别两套话
        await deploy_log_service.append_log(
            db,
            deployment_id=deployment_id,
            step=DeployStep.SCALE,
            level=LogLevel.WARN,
            status=LogStatus.FAILED,
            message=f"扩容失败已回滚（仍 {base} 份）：{getattr(error, 'user_reason', None) or error}",
            extra={"error": str(error)[:500]},
        )
        # 先取消在途 START task 再删行放卡（顺序不可倒）：cancel 执行器自身会 stop+rm
        # 容器，能终结「回滚时容器尚未创建、随后任务继续把它起起来」的竞态——行已删、
        # 账本已放，起来的容器就是无人认领的幽灵占着卡。取消失败 best-effort 放行
        # （下方按容器名直停直删的 _scrub 兜底），不能把回滚本身卡死
        try:
            await StopService().cancel_start_tasks(deployment_id, shards)
        except Exception as e:
            log.warning("scale rollback: 取消在途 START task 失败（best-effort，_scrub 兜底）: %s", e)
        uuids = await _purge_replica_rows(db, deployment_id, engines, shards, doomed)
        await db.flush()
    await orch.ledger.release_cards(deployment_id, set(uuids) | leak_uuids)
    await _release_ram_of_shards(deployment_id, shards)

    # 新份容器可能已起：按容器名 best-effort 直停直删（行已删，task 机制不可用；
    # StopService 按 deployment 停会误伤存量，不能用）。容器间互不依赖，并发清
    async def _scrub(node_id: str, name: str) -> None:
        try:
            await dispatch.stop_container(node_id, name)
            await dispatch.remove_container(node_id, name, force=True)
        except Exception as e:
            log.warning(
                "scale rollback: 容器 %s@%s 清理失败（best-effort，由 node 重启/delete 兜底）: %s",
                name,
                node_id,
                e,
            )

    await asyncio.gather(*[_scrub(node_id, name) for node_id, name in leftovers])
    async with get_db_context() as db:
        await deployment_projection.project_upwards(db, deployment_id)
    return True  # 扩容失败已整份回滚：意图了结
