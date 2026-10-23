"""部署管理路由（推理部署闭环的统一入口：生命周期 + 部署日志）。

全部挂在 ``/admin/model-deployments`` 下，不再散落在 model / deploy_log 两处：

- 生命周期：预检 inspect / 创建 / 列出 / 详情 / 停止 / 启动 / 删除。
- 部署日志（嵌套子资源）：``/{deployment_id}/logs`` 全流程日志、``/{deployment_id}/logs/nodes``
  每节点摘要。日志天然 per-deployment，故 deployment_id 走 path。

容器语义：**stop 只停不删，delete 才删容器**；引擎 stdout 不入库，点容器/node 日志按钮时
经 ``/admin/orchestration/nodes/{node_id}/tasks/tail-log`` 实时拉取。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Path as PathParam, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager.deployment.feasibility import assess_feasibility
from llmpod.apps.manager.deployment.schemas import DeploymentConfig
from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.deployment_inspect import InspectDeploymentRequest, InspectDeploymentResponse
from llmpod.apps.webserver.schemas.out_models import (
    DeployLogOut,
    DeployNodeSummaryItem,
    DeploymentDetailOut,
    ModelDeploymentOut,
)
from llmpod.apps.webserver.schemas.response import PaginatedResponse
from llmpod.apps.manager.deployment.constants import (
    DEPLOYMENT_ACTIVE_STATUSES,
    DEPLOYMENT_STOPPABLE_STATUSES,
    DeployStep,
    DeploymentPhase,
    DeploymentStatus,
    LogLevel,
    LogStatus,
)
from llmpod.core.exceptions import Conflict, LLMPodValidationError, NotFound
from llmpod.utils import format_datetime
from llmpod.db import get_db
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.permission.permissions import (
    MODEL_DEPLOYMENT_CREATE,
    MODEL_DEPLOYMENT_DELETE,
    MODEL_DEPLOYMENT_READ,
    MODEL_DEPLOYMENT_UPDATE,
)
from llmpod.services import deploy_log_service, deployment_service
from llmpod.services.audit_service import audit_action

log = logging.getLogger("llmpod.apps.webserver.routes.admin.deployment")

deployment_router = APIRouter()


class CreateDeploymentRequest(BaseModel):
    cluster_id: int = Field(..., description="集群ID")
    # 1~247：多副本端点名 = 部署名 +「·副本N」后缀（上限 8 字符），列宽 255 收得下
    name: str = Field(..., min_length=1, max_length=247, description="部署名称")
    model_id: int = Field(..., description="模型ID")
    engine: str = Field("vllm", description="引擎：vllm（v1 唯一）")
    spec: DeploymentConfig = Field(
        default_factory=DeploymentConfig, description="部署配置（topology/engine_params/health_check/...）"
    )
    force: bool = Field(
        False, description="强制部署：忽略预检/同子网等启发式约束（显存不足/跨子网仍尝试，由调度器/NCCL 最终裁决）"
    )


class DeploymentActionData(BaseModel):
    """部署 stop/start 后的目标态回执。"""

    deployment_id: str = Field(..., description="部署ID")
    status: str = Field(..., description="操作后的部署状态（stopped / creating）")


class DeploymentDeleteData(BaseModel):
    """部署删除回执（级联清实例/assignment/endpoint/日志）。"""

    deployment_id: str = Field(..., description="部署ID")
    deleted: bool = Field(True, description="删除是否生效")


class ScalePreviewData(BaseModel):
    """副本调整预览（只读；打开对话框即调）。"""

    current_copies: int = Field(..., description="当前份数")
    per_copy_gpus: int = Field(..., description="每份卡数（tp×pp×ep÷nnodes×nnodes）")
    max_feasible_copies: int = Field(..., description="可扩上限（按调度规则试算；等于当前份数表示不可扩）")
    free_gpus: int = Field(..., description="空闲卡数（当前 arch、未被本部署占用）")
    suggested_placement: dict | None = Field(
        None, description="再扩一份的调度预览（gpu_uuids/node_ids；不可扩为 None）"
    )


class ScaleRequest(BaseModel):
    """副本调整请求。"""

    copies: int = Field(..., ge=1, description="目标份数（缩容下限 1；缩到 0 = 停止部署，是另一操作）")


class ScaleActionData(BaseModel):
    """副本调整回执。"""

    deployment_id: str = Field(..., description="部署ID")
    copies: int = Field(..., description="目标份数")
    status: str = Field(..., description="部署状态（running；SCALING 为内部子态）")


class ContainerLogsItem(BaseModel):
    """单实例容器日志尾部（引擎崩溃诊断）。"""

    instance_id: str = Field(..., description="实例ID")
    node_id: str | None = Field(None, description="节点ID（未分配为 None）")
    container_name: str = Field(..., description="容器名（llmpod-{deployment短}-{instance}）")
    status: str = Field(..., description="实例状态（READY/CRASHED/...）")
    logs: str | None = Field(None, description="日志尾部（取不到为 None）")
    error: str | None = Field(None, description="拉取失败原因（成功为 None）")


class InstanceCommandOut(BaseModel):
    """实例实际下发的完整 docker run 命令（容器诊断）。"""

    found: bool = Field(..., description="是否取到启动命令（无 task 行/ payload 不可解析为 False）")
    command: str | None = Field(None, description="完整 docker run 命令（含续行符，可直接复制执行）")
    image: str | None = Field(None, description="容器镜像")
    container_name: str | None = Field(None, description="容器名")
    task_status: str | None = Field(None, description="下发任务状态（SUCCESS/FAILED/...）")
    attempt: int | None = Field(None, description="尝试序号")
    dispatched_at: str | None = Field(None, description="下发时间（ISO8601）")


# ===================== 部署生命周期 =====================


@deployment_router.post(
    "/inspect",
    response_model=Result[InspectDeploymentResponse],
    summary="部署预检（推荐配置 + 校验 + 预览拓扑）",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_CREATE))],
)
async def inspect_deployment(request: InspectDeploymentRequest, db: AsyncSession = Depends(get_db)):
    """部署预检：推荐配置 + 逐卡匹配评估 + 校验能否跑 + 预览拓扑。

    按请求完整度三阶段递进：仅 model_id → 推荐引擎/拓扑/集群；+engine +topology →
    结构校验 + 显存可行性 → deployable/issues；+gpu_selector → 额外校验指定卡集。
    编排与判定逻辑在 ``services/deployment_inspect``（阶段表见其模块 docstring）。
    """
    from llmpod.apps.webserver.services.deployment_inspect import run_inspect

    return success(await run_inspect(db, request))


@deployment_router.post(
    "",
    response_model=Result[ModelDeploymentOut],
    summary="创建部署（触发异步部署）",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_CREATE))],
)
@audit_action(action="create", resource="model_deployment")
async def create_deployment(
    request: CreateDeploymentRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """创建部署。"""
    # 引擎-类别校验（引擎知识，非 DB；先于落库）。需 model_type → 取 model
    from llmpod.apps.manager.deployment.engine import get_adapter

    adapter = get_adapter(request.engine)  # 未知引擎 → LLMPodValidationError（422）
    model = await db.get(Model, request.model_id)
    if model is None:
        raise NotFound("Model", request.model_id)
    adapter.validate_category(model.model_type)

    # CPU 部署守卫（先于结构校验：引擎不支持时按能力报错，不被 node_ids 长度类
    # 结构错误抢先）——能力由适配器声明（llama.cpp 支持，vLLM/SGLang 不支持），
    # 请求了但引擎没这能力直接 422，不静默忽略后按显存部署（部署语义不漂移）
    if request.spec.cpu_lane_requested and not adapter.supports_cpu_lane():
        raise LLMPodValidationError(
            f"引擎 {request.engine} 不支持 CPU / 内存部署（allow_cpu / offload_preference=cpu_only）"
        )
    # 拓扑结构校验（逐份：跨机整除按引擎钩子 / 单 replica 跨机 / gpu_selector 约束 / tp 能否切开
    # 模型权重）→ 422 早拒
    from llmpod.apps.manager.deployment import parallel
    from llmpod.apps.manager.deployment.scheduler.topology import validate_topology

    validate_topology(
        request.spec.topology,
        request.spec.gpu_selector,
        adapter=adapter,
        shape=parallel.shape_for(adapter, model),
        # 显式 cpu_only：node_ids 语义=逐副本指定（放宽 GPU 侧 node_ids×多副本拒绝）。
        # 不再叠加能力判定——上方能力闸已把「不支持 CPU 的引擎 + cpu 请求」422，这里
        # supports_cpu_lane() 恒真（inspect 侧无先拦闸，那边要保留该合取）
        cpu_lane=request.spec.offload_preference == resource.SCENARIO_CPU_ONLY,
    )

    # 手选 GPU 与 CPU 部署互斥（手选即固定显存部署）。判据用 cpu_lane_requested
    # （= allow_cpu 或显式 cpu_only）——只认 allow_cpu 会漏掉「显式 cpu_only + 手选卡」：
    # 该请求被静默接受后调度走纯 CPU 分支，用户手选的卡被丢弃、架构/显存校验也被跳过，
    # 与预览路径（cpu_lane_ok 因手选卡判 False → 报不可部署）结论相反
    if request.spec.cpu_lane_requested and request.spec.gpu_selector and request.spec.gpu_selector.gpu_uuids:
        raise LLMPodValidationError(
            "CPU 部署请求（allow_cpu / offload_preference=cpu_only）与手选 GPU（gpu_selector.gpu_uuids）"
            "互斥：手选卡即按显存部署"
        )
    if request.spec.cpu_lane_requested and request.spec.offload_preference == resource.SCENARIO_CPU_ONLY:
        from llmpod.apps.manager.deployment.scheduler.topology import validate_cpu_lane

        validate_cpu_lane(request.spec.topology)

    # 并行能力校验（pp>1 须引擎声明 + 模型侧门槛；force 放行——引擎最终裁决）
    if not request.force:
        adapter.validate_parallel(request.spec.topology, architecture=model.architecture, model_type=model.model_type)

    # 手选卡 arch×拓扑同步硬拒（判据与文案唯一定义处在 adapter.assert_arch_serves_topology，
    # 编排器 _plan / inspect 手选校验同源）：cann 这类「架构服务不了 pp/多机」的组合，
    # 异步 _plan 虽也会拒，但同步 422 才能兑现「提交即知」——否则落库成 creating 再异步
    # failed，原因埋在 last_error。自动调度路径无需在此拦（schedulable_archs 已剔除）。
    # 池只加载这一次：下方占用预检的 totals 复用同一份（原先各加载一遍全平台在线卡）。
    sel_uuids = request.spec.gpu_selector.gpu_uuids if request.spec.gpu_selector else None
    manual_pool: list | None = None
    if sel_uuids:
        from llmpod.apps.manager.deployment.scheduler.gpu import infer_selection_arch

        manual_pool = await deployment_service.load_online_gpus(db)
        adapter.assert_arch_serves_topology(
            infer_selection_arch(manual_pool, sel_uuids, adapter.default_arch), request.spec.topology
        )

    # force 合并进 spec（异步 _plan 需读它跳过同子网等启发式校验）
    request.spec.force = request.force

    # 可部署性预检（读 DB model + 指定 cluster + max_model_len）：不可部署立即 422，避免落 creating 后异步 failed。
    # force=True 跳过（启发式预检，最终由调度器/NCCL 兜底）；best-effort：feasibility 自身异常不阻塞 deploy。
    if not request.force:
        from llmpod.apps.manager.deployment.scheduler.topology import unit_parallel

        try:
            report = await assess_feasibility(
                db,
                model_size_gb=model.size_gb,
                model_type=model.model_type,
                model_format=model.format,
                cluster_id=request.cluster_id,
                context_size=request.spec.topology.max_model_len,
                kv_dtype_bytes=adapter.kv_cache_dtype_bytes_from(request.spec.engine_params),
                arch_meta=resource.KVArchMeta.from_model(model),
                n_parallel=request.spec.kv_cache.n_parallel,
                total_layers=model.num_layers,
                # 部署授权与逐卡口径：与编排器 _resolve_lane 同参，否则预检放行的请求调度时被拒
                allow_cpu=request.spec.cpu_lane_requested,
                parallel=unit_parallel(request.spec.topology),
            )
            if not report.deployable:
                raise LLMPodValidationError(f"部署预检不通过：{report.message}")
        except LLMPodValidationError:
            raise
        except Exception as e:
            log.warning("create_deployment feasibility precheck failed for model %s: %s", request.model_id, e)

    # 模型带自定义代码（auto_map）且引擎有信任 flag → 自动补进 extra_args（引擎缺它必拒启）
    from llmpod.apps.manager.deployment.engine.base import ensure_trust_remote_code

    request.spec.engine_params = ensure_trust_remote_code(adapter, model, request.spec.engine_params)

    # 引擎参数 overlap 告警（advisory：protected flag 会被参数合并器忽略，不阻塞部署）
    from llmpod.apps.manager.deployment.assess import check_protected_overlap

    extra_args = (request.spec.engine_params or {}).get("extra_args")
    overlap = check_protected_overlap(request.engine, extra_args)
    if overlap:
        log.warning("deployment model=%s extra_args 含系统必控 flag 将被忽略: %s", request.model_id, overlap)

    # 手选卡占用预检（声明+status-aware）：被别的 active 部署占用、剩余装不下 → 409 带明细。
    # 本部署需占 = 生效显存占比 × total（整卡独占默认 0.9，与调度器 card_reserve 同口径）。
    # auto 调度（未手选）不加同步预检——权威判定由 scheduler.reserve（ledger）兜底。
    # 手选时池已在上方 arch 预检加载（manual_pool），此处复用不再查第二遍
    if sel_uuids:
        try:
            util = adapter.memory_utilization_from(request.spec.engine_params)
            occupancy = await deployment_service.resolve_gpu_occupancy(db, sel_uuids)
            totals = {g.uuid: (g.total_vram_gb or 0.0) for g in manual_pool if g.uuid in sel_uuids}
            conflicts = []
            for uuid in sel_uuids:
                e = occupancy.get(uuid)
                if not e or not e.occupied_by:
                    continue
                total = totals.get(uuid, 0.0)
                need = util * total
                remaining = e.remaining_gb(total, adapter.default_gpu_memory_utilization)
                if remaining + 1e-6 < need:
                    conflicts.append(
                        {
                            "gpu_uuid": uuid,
                            "deployment_name": e.occupant_names(),
                            "occupied_pct": e.occupied_pct,
                            "remaining_gb": round(remaining, 2),
                            # 错误详情走 ErrorResult 自由载荷，模型在此 dump 成 dict
                            "occupied_by": [o.model_dump() for o in e.occupied_by],
                        }
                    )
            if conflicts:
                exc = Conflict("选中的 GPU 被占用，剩余显存不足")
                exc.details = {"conflicts": conflicts}
                raise exc
        except Conflict:
            raise
        except Exception as e:
            log.warning("create_deployment occupancy precheck failed for model %s: %s", request.model_id, e)

    dep = await deployment_service.create_deployment(
        db,
        cluster_id=request.cluster_id,
        name=request.name,
        model_id=request.model_id,
        engine=request.engine,
        spec=request.spec,
        trigger=f"user:{user.user_id}",
    )
    await db.refresh(dep)
    await db.commit()

    # 触发部署：desired state(creating) 已写库，唤醒控制循环驱动（替 start_deploy fire-and-forget）
    from llmpod.apps.manager.deployment.controller import get_controller

    get_controller().wake()
    log.info("deployment %s created (engine=%s), controller woken", dep.deployment_id, request.engine)
    return success(dep)


@deployment_router.get(
    "",
    response_model=PaginatedResponse[ModelDeploymentOut],
    summary="列出部署",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def list_deployments(
    cluster_id: int | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    items, total = await deployment_service.list_deployments(db, cluster_id, status, page, page_size)
    return PaginatedResponse.result(items, total, page, page_size)


@deployment_router.get(
    "/{deployment_id}",
    response_model=Result[DeploymentDetailOut],
    summary="部署详情",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def get_deployment(
    deployment_id: Annotated[str, PathParam()],
    db: AsyncSession = Depends(get_db),
):
    dep, insts, endpoints, replica_rows, engine_rows = await deployment_service.get_deployment_detail(db, deployment_id)
    # 副本层 DTO 组装：容器分片按引擎归组、端点按副本反查（服务层出原始行，投影链语义
    # 不进 service）。shards_by_engine 对无挂载列的旧容器行返回空——旧数据副本层自然为空
    from llmpod.apps.webserver.schemas.out_models import DeploymentReplicaOut, EngineInstanceOut
    from llmpod.services.deployment_projection import engines_by_replica, shards_by_engine

    grouped = shards_by_engine(insts)
    by_replica = engines_by_replica(engine_rows)
    endpoint_ids_by_replica: dict[str, list[int]] = {}
    for ep in endpoints:
        if ep.model_deployment_replica_id is not None:
            endpoint_ids_by_replica.setdefault(ep.model_deployment_replica_id, []).append(ep.id)
    replicas_out = [
        DeploymentReplicaOut(
            replica_id=r.replica_id,
            seq=r.seq,
            status=r.status,
            engines=[
                EngineInstanceOut(
                    instance_id=e.instance_id,
                    role=e.role,
                    tp=e.tp,
                    pp=e.pp,
                    ep=e.ep,
                    nnodes=e.nnodes,
                    gpu_arch=e.gpu_arch,
                    status=e.status,
                    shard_ids=[s.instance_id for s in grouped.get(e.instance_id, [])],
                )
                for e in sorted(by_replica.get(r.replica_id, []), key=lambda x: x.role)
            ],
            endpoint_ids=sorted(endpoint_ids_by_replica.get(r.replica_id, [])),
        )
        for r in replica_rows
    ]
    detail = DeploymentDetailOut(
        deployment=dep,
        instances=insts,
        endpoints=endpoints,
        replicas=replicas_out,
    )
    # last_error 仅 creating/running（启动中/运行中）隐藏——此时不该让用户误读成本轮又失败；
    # stopped/failed 保留（手动停止/重启不清库，失败原因可溯源）
    if dep.status in DEPLOYMENT_ACTIVE_STATUSES:
        detail.deployment.last_error = None
    return success(detail)


@deployment_router.post(
    "/{deployment_id}/stop",
    response_model=Result[DeploymentActionData],
    summary="停止部署",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_UPDATE))],
)
@audit_action(action="update", resource="model_deployment", resource_id="deployment_id")
async def stop_deployment(
    deployment_id: Annotated[str, PathParam()],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """停止部署：标 stopped + 取消进行中的编排 + 任务化停止容器（先标 stopped 让 deploy 见到终态不覆盖）。"""
    dep, _model = await deployment_service.load_deployment(db, deployment_id)
    if dep.status not in DEPLOYMENT_STOPPABLE_STATUSES:
        raise Conflict(f"部署当前状态 {dep.status}，无法停止")
    # 先标 stopped + phase STOPPING + commit：进行中的 deploy/finalize 见到终态即不覆盖
    dep.status = DeploymentStatus.STOPPED
    dep.health_status = None  # 运行时健康仅在 running 期有意义，离开即清
    await deploy_log_service.append_log(
        db,
        deployment_id=deployment_id,
        step=DeployStep.STOP,
        level=LogLevel.WARN,
        status=LogStatus.RUNNING,
        message="手动停止部署",
        extra={"trigger": f"user:{user.user_id}", "action": "stop"},
    )
    await deployment_service.set_phase(db, deployment_id, DeploymentPhase.STOPPING, trigger=f"user:{user.user_id}")
    await db.commit()

    from llmpod.apps.manager.deployment.orchestrator import get_orchestrator
    from llmpod.apps.manager.deployment.stop import StopService

    orch = get_orchestrator()
    # 取消并等 staging 阶段协程退出（容器阶段 deploy 协程已交接返回，此处 no-op）
    await orch.request_cancel(deployment_id)
    await orch.await_deploy(deployment_id, timeout=30.0)
    # 任务化停止：cancel START task → STOP_INSTANCE task（纯停）→ 轮询 STOPPED。stop 不删容器。
    # 停止未确认（节点离线/超时）时**不得**释放 VRAM/删 assignment——容器可能还在运行，
    # 释放会把同卡调度给别人（双容器抢卡）。留 stopped 终态交 controller converge 下轮
    # 继续收敛（converge 对未确认部署不释放，见 converge_stop_deployment）。
    all_stopped = await StopService().stop_instances(deployment_id)
    if all_stopped:
        # 释放 VRAM 预留（stop 也释放——无运行容器占卡，回收调度额度）
        await deployment_service.delete_assignments(db, deployment_id)
    else:
        await deploy_log_service.append_log(
            db,
            deployment_id=deployment_id,
            step=DeployStep.STOP,
            level=LogLevel.WARN,
            status=LogStatus.RUNNING,
            message="部分实例停止未确认（节点离线/超时），VRAM 暂不释放，待收敛后再回收",
        )
    # stop 联动下架：本部署唯一来源的自动发布路由 → unpublished（调用 404 而非 503）
    await deployment_service.unpublish_deployment_routes(db, deployment_id)
    await db.commit()
    if all_stopped:
        await orch.release_vram(deployment_id)
    # 停止后重算模型分发需求：无人再需要的下载被取消（仍被其他部署需要的不误杀）
    from llmpod.apps.manager.deployment.placement import PlacementService

    try:
        await PlacementService().recompute_demand()
    except Exception:
        log.exception("recompute_demand after stop failed (best-effort)")
    return success(DeploymentActionData(deployment_id=deployment_id, status=DeploymentStatus.STOPPED))


@deployment_router.post(
    "/{deployment_id}/start",
    response_model=Result[DeploymentActionData],
    summary="启动（重启）部署",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_UPDATE))],
)
@audit_action(action="update", resource="model_deployment", resource_id="deployment_id")
async def start_deployment(
    deployment_id: Annotated[str, PathParam()],
    force: bool = Query(False, description="原卡被其他部署占用时强制启动（抢占，自负 OOM 风险）"),
    user: SessionData = Depends(get_current_user),
):
    """启动已停止/失败的部署：在原卡重新预留 VRAM → 重置 → 控制器重部署。

    - 仅 stopped/failed 可启动；
    - 原卡被别的部署占用 → 409（``data.occupied`` 明细：哪张卡被哪个部署占了多少显存），
      前端据此提示用户；带 ``force=true`` 则强制抢占重启。
    """
    from llmpod.apps.manager.deployment.orchestrator import get_orchestrator

    await get_orchestrator().restart(deployment_id, force=force, trigger=f"user:{user.user_id}")
    return success(DeploymentActionData(deployment_id=deployment_id, status=DeploymentStatus.CREATING))


@deployment_router.post(
    "/{deployment_id}/scale/preview",
    response_model=Result[ScalePreviewData],
    summary="副本调整预览（只读）",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def scale_preview(
    deployment_id: Annotated[str, PathParam()],
    db: AsyncSession = Depends(get_db),
):
    """扩缩容预检：可扩上限 + 选卡预览（调度管线算出不落库）/ 缩容将移除的份。"""
    from llmpod.apps.manager.deployment.scale import preview_scale

    data = await preview_scale(db, deployment_id)
    return success(ScalePreviewData(**data))


@deployment_router.post(
    "/{deployment_id}/scale",
    response_model=Result[ScaleActionData],
    summary="在线调整副本数",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_UPDATE))],
)
@audit_action(action="update", resource="model_deployment", resource_id="deployment_id")
async def scale_deployment_route(
    deployment_id: Annotated[str, PathParam()],
    body: ScaleRequest,
    user: SessionData = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """在线扩缩：扩容全自动调度（失败只回滚新份）；缩容 LIFO 摘端点→排空→停容器。

    仅 running 可调（SCALING 中 409）；副本内规格不可变（改规格 = 删了重部）。
    本接口只写 desired state（意图 + phase=SCALING + 日志）后唤醒控制循环，实际推进由
    controller 的驱动协程完成——进程内唯一执行者，故并发提交不会双扩。目标份数与当前
    相同视为无操作，直接返回成功。
    """
    from llmpod.apps.manager.deployment import scale as scale_mod
    from llmpod.apps.manager.deployment.controller import get_controller

    await scale_mod.request_scale(db, deployment_id, body.copies, trigger=f"user:{user.user_id}")
    await db.commit()
    # desired state 已落库，唤醒控制循环驱动（与 create 路由同一口径）
    get_controller().wake()
    return success(ScaleActionData(deployment_id=deployment_id, copies=body.copies, status=DeploymentStatus.RUNNING))


@deployment_router.post(
    "/{deployment_id}/delete",
    response_model=Result[DeploymentDeleteData],
    summary="删除部署（守卫）",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_DELETE))],
)
@audit_action(action="delete", resource="model_deployment", resource_id="deployment_id")
async def delete_deployment(
    deployment_id: Annotated[str, PathParam()],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """删除部署：status=deleting → cancel 编排 → 任务化停止容器 → 删 instance/assignment/endpoint → 删部署。"""
    dep, _model = await deployment_service.load_deployment(db, deployment_id)
    insts = await deployment_service.list_instances(db, deployment_id)
    if dep.status == DeploymentStatus.DELETING:
        raise Conflict("部署正在删除中")

    # 1. 标 deleting + phase STOPPING + 取消进行中的编排
    dep.status = DeploymentStatus.DELETING
    dep.health_status = None  # 运行时健康仅在 running 期有意义，离开即清
    await deployment_service.set_phase(db, deployment_id, DeploymentPhase.STOPPING, trigger=f"user:{user.user_id}")
    await db.commit()
    from llmpod.apps.manager.deployment.orchestrator import get_orchestrator
    from llmpod.apps.manager.deployment.stop import StopService

    orch = get_orchestrator()
    await orch.request_cancel(deployment_id)
    # 等 deploy 任务退出再清行——避免 DELETE 与后台 deploy 并发改 status
    await orch.await_deploy(deployment_id, timeout=30.0)

    # 2. 任务化停止容器（cancel START → STOP_INSTANCE 纯停 → 轮询 STOPPED），
    #    再显式删容器（delete 独有：label 扫除 cleanup；stop 部署只停不删）
    await StopService().stop_instances(deployment_id)
    node_ids = {i.assigned_node for i in insts if i.assigned_node}
    await orch.cleanup_deployment_containers(deployment_id, node_ids)

    # 3. 删关联行（无 DB FK，显式删）+ 4. 释放 VRAM 预留（同步进程内 ledger）
    await deployment_service.delete_deployment_cascade(db, deployment_id)
    await db.commit()
    await orch.release_vram(deployment_id)
    # 删除后重算模型分发需求：无人再需要的下载被取消（仍被其他部署需要的不误杀）
    from llmpod.apps.manager.deployment.placement import PlacementService

    try:
        await PlacementService().recompute_demand()
    except Exception:
        log.exception("recompute_demand after delete failed (best-effort)")
    return success(DeploymentDeleteData(deployment_id=deployment_id))


# ===================== 部署全流程日志（deploy_log 只读，嵌套在部署下）=====================


@deployment_router.get(
    "/{deployment_id}/logs",
    response_model=PaginatedResponse[DeployLogOut],
    summary="部署全流程日志",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def list_deployment_logs(
    deployment_id: str,
    node_id: str | None = Query(None, description="节点ID（多机时按机器过滤）"),
    task_id: str | None = Query(None, description="任务ID"),
    step: str | None = Query(None, description="步骤：submit/staging_model/pull_image/start_head/..."),
    level: str | None = Query(None, description="级别：debug | info | warn | error"),
    status: str | None = Query(None, description="步骤状态：pending | running | success | failed | cancelled"),
    since: datetime | None = Query(None, description="起始时间（含，ISO8601）"),
    until: datetime | None = Query(None, description="截止时间（含，ISO8601）"),
    search: str | None = Query(None, description="message 关键字（大小写不敏感包含）"),
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(100, ge=1, le=500, description="每页条数，最大500"),
    db: AsyncSession = Depends(get_db),
):
    """某部署的全流程日志（前端部署详情「日志」时间线主视图，按 created_at 升序）。

    日志天然 per-deployment，故 deployment_id 走 path（不再单独 query 列表）。引擎 stdout
    不入库——点容器/node 日志按钮时经 ``/admin/orchestration/nodes/{node_id}/tasks/tail-log`` 实时拉取。
    """
    items, total = await deploy_log_service.list_logs(
        db,
        deployment_id=deployment_id,
        node_id=node_id,
        task_id=task_id,
        step=step,
        level=level,
        status=status,
        since=since,
        until=until,
        search=search,
        page=page,
        page_size=page_size,
    )
    return PaginatedResponse.result(items, total, page, page_size)


@deployment_router.get(
    "/{deployment_id}/logs/containers",
    response_model=Result[list[ContainerLogsItem]],
    summary="部署容器日志（引擎 stdout/stderr 尾部）",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def deployment_container_logs(
    deployment_id: str,
    tail: int = Query(200, ge=1, le=2000, description="每个容器取尾部行数"),
    db: AsyncSession = Depends(get_db),
):
    """逐实例实时拉容器日志尾部（引擎崩溃诊断：readiness 期 ExitCode!=0 时看 stderr）。

    引擎 stdout 不入库，经 node 隧道 ContainerService.logs 实拉；容器已退出仍可取
    （docker logs 对 exited 容器有效）。单节点失败只污染对应项 error，不影响其余。
    """
    from llmpod.apps.manager import dispatch

    items = await dispatch.container_logs(db, deployment_id, tail=tail)
    return success([ContainerLogsItem(**i) for i in items])


@deployment_router.get(
    "/{deployment_id}/instances/{instance_id}/command",
    response_model=Result[InstanceCommandOut],
    summary="实例启动命令（实际下发的完整 docker run）",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def deployment_instance_command(
    deployment_id: str,
    instance_id: str,
    db: AsyncSession = Depends(get_db),
):
    """该实例容器启动时实际下发的完整 docker run（排障：节点上手工复现/比对）。

    命令取自 START 任务下发那一刻的 DeploySpec 快照（task.payload），非按当前配置
    重算——部署后 catalog/配置漂移不影响回显。无 task 行（重启窗口/未下发过）返回
    ``found=false`` 而非报错。
    """
    from llmpod.apps.manager import dispatch

    item = await dispatch.instance_start_command(db, deployment_id, instance_id)
    return success(InstanceCommandOut(**item))


@deployment_router.get(
    "/{deployment_id}/logs/nodes",
    response_model=Result[list[DeployNodeSummaryItem]],
    summary="部署每节点当前步骤/状态",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def deployment_node_summary(
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
):
    """每节点当前在哪一步 / 什么状态 / 最后一条消息（多机部署的节点级进度直览）。

    进度数值不加专用字段：拉取/下载进度与模型下载同构，走 task 上报 →
    deploy_log 时间线行（消息文本带「已拉取/已下载 x/y GB」），用户看日志即可。
    """
    rows = await deploy_log_service.latest_log_by_node(db, deployment_id)
    node_ids = {r.node_id for r in rows if r.node_id}
    display = await deploy_log_service.node_display_map(db, node_ids)
    items: list[DeployNodeSummaryItem] = []
    for r in rows:
        host, ip = display.get(r.node_id, (None, None))
        items.append(
            DeployNodeSummaryItem(
                node_id=r.node_id,
                node_hostname=host,
                node_ip=ip,
                step=r.step,
                status=r.status,
                level=r.level,
                message=r.message,
                updated_at=format_datetime(r.created_at),
            )
        )
    return success(items)


@deployment_router.get(
    "/{deployment_id}/stats",
    response_model=Result[list],
    summary="部署历史指标曲线",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def deployment_metrics_series(
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
    from_: Annotated[datetime | None, Query(alias="from", description="起始时间 ISO8601")] = None,
    to: Annotated[datetime | None, Query(description="结束时间 ISO8601")] = None,
    granularity: Annotated[str, Query(description="auto | 5m | 1h | 1d")] = "auto",
):
    """单部署历史曲线（metric_rollup 过滤 deployment_id；点结构与 /admin/dashboard 一致）。"""
    from llmpod.services import metric_query_service

    series = await metric_query_service.dimension_series(
        db,
        dim="deployment",
        dim_id=deployment_id,
        from_=from_ or datetime(1970, 1, 1, tzinfo=timezone.utc),
        to=to or datetime.now(timezone.utc),
        granularity=granularity,
    )
    return success(series)


@deployment_router.get(
    "/{deployment_id}/engine-status",
    response_model=Result[list],
    summary="部署实时引擎状态",
    dependencies=[Depends(require_permission(MODEL_DEPLOYMENT_READ))],
)
async def deployment_engine_metrics_realtime(
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
):
    """部署实时引擎状态（KV cache/排队/命中率）：中央 scraper 内存快照直读。

    实例列表查 Instance 表，实时值从 scraper 抓取引擎 /metrics 后的 last-value
    缓存取（与 scraper 同进程，无跨进程开销）；实例从未抓到/引擎下线 →
    ok=False 或指标缺失，不报错。``age_ms`` 是快照年龄——引擎抓取按「面板
    是否被消费」自动升降档，首屏可能是慢档旧值（最长一个慢档间隔），照实
    标注不假装实时。消费即触碰需求信号（引擎抓取升快档），前端无需传参。
    """
    from llmpod.apps.webserver.metrics_scraper import get_scraper, touch_scraper_demand
    from llmpod.utils.time import now_local

    touch_scraper_demand()
    insts = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
    scraper = get_scraper()
    snapshots = (
        {s.instance_id: s for s in scraper.engine_snapshots_by_deployment(deployment_id)} if scraper is not None else {}
    )
    now = now_local()
    items = []
    for inst in insts:
        s = snapshots.get(inst.instance_id)
        items.append(
            {
                "instance_id": inst.instance_id,
                "rank": inst.rank,
                "status": inst.status,
                "ok": bool(s and s.ok),
                # headless worker（endpoint 为空，无 API 端口）不暴露 /metrics 属预期，
                # 前端据此区分「流水线 worker」与「真抓取失败」
                "metrics_expected": inst.endpoint is not None,
                "kv_cache_util": s.kv_cache_util if s else None,
                "running_requests": s.running_requests if s else None,
                "waiting_requests": s.waiting_requests if s else None,
                "prefix_cache_hit_rate": s.prefix_cache_hit_rate if s else None,
                "num_preemptions": s.num_preemptions if s else None,
                "age_ms": round((now - s.fetched_at).total_seconds() * 1000) if s and s.fetched_at else None,
            }
        )
    return success(items)
