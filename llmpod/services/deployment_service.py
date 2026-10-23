"""部署域 DB 服务（纯 async 函数，首参 ``AsyncSession``）。

遵循 ``routes → services → models`` 分层：orchestrator（协调 adapter/executor/cancel/容器清理）
与 ``admin/model`` 路由（HTTP + 鉴权）共用此服务做部署相关的 DB 读写——两者都不再内联 ORM。

约定：
- 首参 ``db: AsyncSession``；
- 写入后只 ``flush``（让调用方控制 commit 时机：路由 commit，orchestrator 在其 session 内 flush）；
- 不做 arch/引擎/容器决策（那是 orchestrator + adapter 的职责），只做纯 DB 读写 + 领域校验。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.schemas import (
    CardSummary,
    CardOccupant,
    DeploymentConfig,
    GpuAssignment,
    GpuCandidate,
    GpuOccupancy,
    InstanceSpec,
    NodeCandidate,
    OccupiedBy,
    PlacementPrefs,
)
from llmpod.apps.manager.deployment.constants import (
    DEPLOYMENT_ACTIVE_STATUSES,
    DEPLOYMENT_FAILABLE_STATUSES,
    DEPLOYMENT_TERMINAL_STATUSES,
    CardState,
    INSTANCE_PORT_RELEASED_STATUSES,
    INSTANCE_TRANSIENT_STATUSES,
    RENDEZVOUS_ISOLATION_BAND,
    RENDEZVOUS_PORT_RANGE,
    SERVICE_PORT_RANGE,
    DeployStep,
    DeploymentPhase,
    DeploymentStatus,
    EngineStatus,
    HealthStatus,
    LogLevel,
    LogStatus,
    NodeModelState,
    ReplicaStatus,
)
from llmpod.config.constants import GpuStatus, InstanceStatus, NodeStatus
from llmpod.core.exceptions import Conflict, LLMPodError, NotFound
from llmpod.apps.manager.deployment.resource_ledger import DEFAULT_GPU_MEMORY_UTILIZATION
from llmpod.models.deployment_gpu_assignment import DeploymentGpuAssignment
from llmpod.models.gpu import Gpu
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.model_deployment_instance import ModelDeploymentInstance
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.node import Node
from llmpod.models.node_model import NodeModel
from llmpod.models.route import Route
from llmpod.models.route_binding import RouteBinding
from llmpod.services import deploy_log_service, deployment_projection
from llmpod.services.publish_service import delete_route
from llmpod.utils.crypto import generate_uuid


# ---- 读 ----


class LoadedDeployment(NamedTuple):
    """load_deployment 的返回（部署行 + 其模型行，位置解包兼容）。"""

    dep: ModelDeployment
    model: Model


class DeploymentDetail(NamedTuple):
    """get_deployment_detail 的返回（endpoints 可空列表：一协议一行）。

    ``replicas``/``engines`` 为四层模型的中间两层原始行（副本按 seq 排序；引擎带
    replica_id 归属）——DTO 组装（shard/endpoint 反查）在 webserver 层做。四层化前
    的旧数据两者为空列表。
    """

    dep: ModelDeployment
    instances: list[Instance]
    endpoints: list[ModelEndpoint]
    replicas: list[ModelDeploymentReplica]
    engines: list[ModelDeploymentInstance]


async def load_deployment(db: AsyncSession, deployment_id: str) -> LoadedDeployment:
    """加载部署 + 模型（编排前置；任一缺失 raise NotFound）。"""
    dep = await db.get(ModelDeployment, deployment_id)
    if dep is None:
        raise NotFound("ModelDeployment", deployment_id)
    model = await db.get(Model, dep.model_id)
    if model is None:
        raise NotFound("Model", dep.model_id)
    return LoadedDeployment(dep=dep, model=model)


async def load_online_gpus(db: AsyncSession) -> list[GpuCandidate]:
    """在线 GPU 池（调度候选），投影成 GpuCandidate 供 GpuScheduler 消费。

    typed model 在 DB→调度边界即固定字段契约，避免 dict key 拼写错误只在 deploy 期暴露。
    ``used_vram_gb`` 取心跳实时值（``feasibility.load_used_vram_by_uuid``，进程内 buffer，
    不查 DB）——它是容量过滤的「物理可用」闸的数据源：外部进程占着的卡不能只看账本，
    否则账本说空、放行调度、容器起来 OOM。
    """
    from llmpod.apps.manager.deployment.feasibility import load_used_vram_by_uuid

    used = load_used_vram_by_uuid()
    result = await db.execute(select(Gpu).where(Gpu.status == GpuStatus.ONLINE))
    return [
        GpuCandidate(
            uuid=g.uuid,
            node_id=g.node_id,
            index=g.index,
            arch=g.arch,
            total_vram_gb=g.total_vram_gb,
            status=g.status,
            name=g.name,
            driver_version=g.driver_version,
            used_vram_gb=used.get(g.uuid),
        )
        for g in result.scalars()
    ]


async def nodes_with_model(db: AsyncSession, model_id: int) -> set[str]:
    """已有该模型文件的节点集合（调度本地性偏好的判定依据）。

    只认 ``NodeModel.status == PRESENT``（节点上报的缓存态）。调度偏好用缓存态是合适的：
    这里回答的是「这台机器上现在有没有这份文件」，判错最多是少省一次下发或多下一次；
    真正的放行门（能不能起容器）仍由 staging 的**本轮复核任务终态**把关（见
    ``placement.ModelStagingGate``），两者不共用判据。
    """
    rows = list((await db.execute(select(NodeModel).where(NodeModel.model_id == model_id))).scalars())
    return {r.node_id for r in rows if r.status == NodeModelState.PRESENT}


async def load_online_nodes(db: AsyncSession, cluster_id: int | None = None) -> list[NodeCandidate]:
    """在线节点池（CPU 部署调度候选）：投影成 NodeCandidate（含内存口径）。

    ``cluster_id`` 非空时只返回该集群节点——CPU 调度（binpack）与预检的节点池同为
    集群作用域，不过滤会把副本落到部署集群之外的节点上。内存取 ``system_buffer``
    实时值、0/缺失回退 DB ``Node.memory_gb``——读心跳的规则与预检的节点事实同一
    入口（``feasibility.load_ram_by_node``），避免两套「节点可用内存」。
    """
    from llmpod.apps.manager.deployment.feasibility import load_ram_by_node, node_ram_total

    stmt = select(Node).where(Node.status == NodeStatus.ONLINE)
    if cluster_id is not None:
        stmt = stmt.where(Node.cluster_id == cluster_id)
    result = await db.execute(stmt)
    ram_total_by_node, _ram_used = load_ram_by_node()
    out: list[NodeCandidate] = []
    for n in result.scalars():
        memory, _realtime = node_ram_total(ram_total_by_node, n.id, n.memory_gb)
        out.append(NodeCandidate(node_id=n.id, hostname=n.hostname or "", ip=n.ip or "", memory_gb=memory))
    return out


async def load_node_ips(db: AsyncSession, node_ids: set[str]) -> dict[str, str]:
    """node_id → ip（rank 解析用）；空集返回 {}。"""
    if not node_ids:
        return {}
    result = await db.execute(select(Node).where(Node.id.in_(node_ids)))
    return {n.id: n.ip for n in result.scalars()}


async def load_node_net_ifnames(db: AsyncSession, node_ids: set[str]) -> dict[str, str]:
    """node_id → 持有 ip 的物理网卡名（多机部署 SOCKET_IFNAME 注入用）；未上报为空串。"""
    if not node_ids:
        return {}
    result = await db.execute(select(Node).where(Node.id.in_(node_ids)))
    return {n.id: n.net_ifname or "" for n in result.scalars()}


async def list_deployments(
    db: AsyncSession,
    cluster_id: int | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[ModelDeployment], int]:
    """分页列出部署（可选 cluster_id / status 过滤）。"""
    stmt = select(ModelDeployment)
    cnt = select(func.count(ModelDeployment.deployment_id))
    if cluster_id:
        stmt = stmt.where(ModelDeployment.cluster_id == cluster_id)
        cnt = cnt.where(ModelDeployment.cluster_id == cluster_id)
    if status:
        stmt = stmt.where(ModelDeployment.status == status)
        cnt = cnt.where(ModelDeployment.status == status)
    total = (await db.execute(cnt)).scalar() or 0
    stmt = stmt.order_by(ModelDeployment.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    items = list((await db.execute(stmt)).scalars())
    return items, total


async def get_deployment_detail(db: AsyncSession, deployment_id: str) -> DeploymentDetail:
    """部署详情：dep + instances + endpoints + replicas（四层视图组装）。

    副本层组装：replica 行（按 seq）→ 引擎行 → 容器分片按 ``model_deployment_instance_id``
    归组 → 端点按 ``model_deployment_replica_id`` 反查。四层化前的旧数据无副本行，
    ``replicas`` 为空列表（前端回退平铺分组）。
    """
    dep = await db.get(ModelDeployment, deployment_id)
    if dep is None:
        raise NotFound("ModelDeployment", deployment_id)
    insts = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
    endpoints = list(
        (await db.execute(select(ModelEndpoint).where(ModelEndpoint.deployment_id == deployment_id))).scalars()
    )
    replicas = await deployment_projection.replica_rows_of(db, deployment_id)
    replicas.sort(key=lambda r: r.seq)
    engines = await deployment_projection.engine_rows_of(db, [r.replica_id for r in replicas])
    return DeploymentDetail(dep=dep, instances=insts, endpoints=endpoints, replicas=replicas, engines=engines)


async def list_instances(db: AsyncSession, deployment_id: str) -> list[Instance]:
    """某部署的全部 instance（stop/delete 取 assigned_node 清容器用）。"""
    return list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())


# ---- 写 ----


async def create_deployment(
    db: AsyncSession,
    *,
    cluster_id: int,
    name: str,
    model_id: int,
    engine: str,
    spec: DeploymentConfig,
    trigger: str = "system",
) -> ModelDeployment:
    """创建部署行（生成 deployment_id；校验 model 存在）。

    引擎-类别校验（adapter.validate_category）由调用方在调本函数前完成（属引擎知识，非 DB）。
    spec 为 DeploymentConfig，model_dump() 进 JSONB。``trigger`` 记录提交来源（路由传
    ``user:<uid>``），写入 deploy_log submit 事件。
    """
    if await db.get(Model, model_id) is None:
        raise NotFound("Model", model_id)
    dep = ModelDeployment(
        deployment_id=generate_uuid(),
        cluster_id=cluster_id,
        name=name,
        model_id=model_id,
        engine=engine,
        status=DeploymentStatus.CREATING,
        spec=spec.model_dump(),
    )
    db.add(dep)
    await db.flush()
    await deploy_log_service.append_log(
        db,
        deployment_id=dep.deployment_id,
        step=DeployStep.SUBMIT,
        level=LogLevel.INFO,
        status=LogStatus.PENDING,
        message=f"部署已提交：{name}",
        extra={"trigger": trigger, "engine": engine, "model_id": model_id},
    )
    await deploy_log_service.append_log(
        db,
        deployment_id=dep.deployment_id,
        step=DeployStep.QUEUED,
        level=LogLevel.INFO,
        status=LogStatus.PENDING,
        message="排队中，等待调度",
        extra={"trigger": trigger},
    )
    return dep


async def persist_topology(
    db: AsyncSession,
    replicas: list[ModelDeploymentReplica],
    engines: list[ModelDeploymentInstance],
) -> None:
    """持久化副本/引擎两层行（provisioning 初值，由 orchestrator 展开构造）。

    先于容器行写：容器经 ``model_deployment_instance_id`` 挂引擎，status 只归投影链
    （services/deployment_projection）与 restart 重置改写。
    """
    for r in replicas:
        db.add(r)
    for e in engines:
        db.add(e)


async def persist_instances(db: AsyncSession, deployment_id: str, instances: list[InstanceSpec]) -> None:
    """持久化容器 Instance 行（PENDING，带 rank/挂引擎/assigned_node + 展开后的 spec 快照）。"""
    for inst in instances:
        db.add(
            Instance(
                instance_id=inst.instance_id,
                deployment_id=deployment_id,
                model_deployment_instance_id=inst.engine_instance_id or None,
                role=inst.role,
                rank=inst.rank,
                assigned_node=inst.assigned_node,
                status=InstanceStatus.PENDING,
                spec=inst.model_dump(),
            )
        )


async def persist_assignments(
    db: AsyncSession,
    assignments: list[GpuAssignment],
    deployment_id: str,
    vram_per_gpu: float,
    arch: str,
) -> None:
    """持久化 DeploymentGpuAssignment 行（VRAM 预留的权威来源）。

    allocated_vram_gb 取自 ``GpuAssignment.allocated``（逐卡预留 = 生效占比×total，与引擎 cap
    对齐，共享才安全）；gpu_memory_utilization：生效显存占比（引擎 flag 解析，缺省 0.9）。
    须与 scheduler 的 card_reserve 同口径（ledger.rebuild_from_db 读本列）。
    """
    for a in assignments:
        util = a.gpu_memory_utilization  # 生效显存占比或 None（None 持久化写引擎默认 0.9）
        for uuid in a.gpu_uuids:
            allocated = a.allocated.get(uuid, vram_per_gpu)
            db.add(
                DeploymentGpuAssignment(
                    deployment_id=deployment_id,
                    gpu_uuid=uuid,
                    instance_id=a.instance_id,
                    gpu_memory_utilization=util if util is not None else DEFAULT_GPU_MEMORY_UTILIZATION,
                    allocated_vram_gb=allocated,
                    resource_type=arch,
                )
            )


async def set_instance_status(db: AsyncSession, instance_id: str, status: str) -> None:
    """单 instance 状态写回（编排/停止流程用）。

    守卫：部署已终态（stopped/deleting/failed）时拒绝瞬态复活写入
    （PENDING/STARTING/RUNNING/READY——stale 上报/reconciler 不得复活实例）；
    停止路径的 STOPPING/STOPPED 与 FAILED 不受限。
    """
    iobj = await db.get(Instance, instance_id)
    if iobj is None:
        return
    if status in INSTANCE_TRANSIENT_STATUSES:
        dep = await db.get(ModelDeployment, iobj.deployment_id)
        if dep is not None and dep.status in DEPLOYMENT_TERMINAL_STATUSES:
            return
    iobj.status = status
    await db.flush()


async def set_phase(
    db: AsyncSession,
    deployment_id: str,
    phase: str,
    *,
    trigger: str = "reconciler",
    attempt: int | None = None,
    message: str | None = None,
) -> None:
    """部署阶段写回（编排/reconciler 推进可观测子态）+ 记一条 deploy_log。

    重新 fetch dep（编排器持有的 dep 跨已关闭 session，detached 不可写）。无行跳过。
    守卫：同相重写幂等跳过（兼防 reconciler 心跳重复刷日志）——带 ``attempt`` 重试标记的
    同相重写例外，落重试行；部署已终态（stopped/deleting/failed）时不再推进——唯一例外
    是停止路径显式写 ``STOPPING``。
    ``trigger`` 记录来源：编排内部默认 ``reconciler``，stop/delete 路由传 ``user:<uid>``。
    ``attempt`` 供重试标注第几次重试（>1 才追加；同相重试也由此落日志，见守卫）。
    ``message`` 覆盖默认阶段文案：秒过的阶段（模型已缓存，staging 立即完成）提示
    「已存在，跳过下载」，避免显示误导性的「开始下载模型」。
    """
    dep = await db.get(ModelDeployment, deployment_id)
    if dep is None:
        return
    is_retry = attempt is not None and attempt > 1
    if dep.phase == phase and not is_retry:
        return
    if dep.status in DEPLOYMENT_TERMINAL_STATUSES and phase != DeploymentPhase.STOPPING:
        return
    dep.phase = phase
    if message is None:
        # 重试阶段行文案由共享 helper 拼装（reconciler 同相重试也经本函数，见守卫）
        message = (
            deploy_log_service.phase_retry_message(phase, attempt)
            if is_retry
            else deploy_log_service.phase_to_message(phase)
        )
    extra: dict = {"trigger": trigger, "phase": phase}
    if is_retry:
        extra["attempt"] = attempt
    await deploy_log_service.append_log(
        db,
        deployment_id=deployment_id,
        step=deploy_log_service.phase_to_step(phase),
        # level 来自 deploy_log_service 投影（STOPPING=warn，其余 info）+ 重试提升 warn
        level=LogLevel.WARN if is_retry else deploy_log_service.phase_to_level(phase),
        status=deploy_log_service.phase_to_log_status(phase),
        message=message,
        extra=extra,
    )
    await db.flush()


async def persist_running(
    db: AsyncSession,
    deployment_id: str,
    instance_ids: list[str],
    endpoints: list[ModelEndpoint],
    api_base: str | None,
) -> None:
    """部署达成落库：写 ModelEndpoint（一副本一协议一行） + dep→running。

    重新 fetch dep（编排器持有的 dep 来自已关闭 session，detached）；endpoint 由编排器据适配器构造。
    若 dep 已被 stop/delete 抢先置终态（stopped/deleting/failed）则 raise——编排器据此不覆盖成 running。
    endpoint 幂等（重复 finalize，如崩溃恢复后再进 FINALIZING）由编排器预查已有行提前返回；
    本函数无条件 add——只在新 finalize 路径被调用（唯一调用方已保证不存在同键行）。

    四层达成态**只认领本次达成的实例集合**：扩容场景下存量副本可能正 down/
    崩溃自愈中，盲盖会把投影链刚写下的真实状态抹掉（状态的权威来源在容器上报
    与投影链，这里只对「全部容器都本次达成」的引擎/副本置 ready/serving）。
    健康灯同样按副本现状算，不无条件 HEALTHY。
    """
    dep = await db.get(ModelDeployment, deployment_id)
    if dep is None:
        raise LLMPodError(f"部署在达成确认前消失: {deployment_id}")
    if dep.status in DEPLOYMENT_TERMINAL_STATUSES:
        raise LLMPodError(f"部署 {deployment_id} 已处于终态 {dep.status}，跳过达成确认")
    for ep in endpoints:
        db.add(ep)
    dep.status = DeploymentStatus.RUNNING
    dep.api_endpoint = api_base
    dep.last_error = None  # 成功部署清空历史失败原因
    dep.crash_count = 0
    dep.last_crash_at = None

    # 引擎/副本达成判定：成员容器全部 ∈ 本次达成集合。分片一次取全（达成写 READY 与
    # 分组共用一批行，不再逐 iid 重复 get）
    all_shards = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
    achieved = set(instance_ids)
    for s in all_shards:
        if s.instance_id in achieved:
            s.status = InstanceStatus.READY
    shards_by_engine = deployment_projection.shards_by_engine(all_shards)
    fully_ready_engines = [
        eng_id
        for eng_id, members in shards_by_engine.items()
        if members and all(m.instance_id in achieved for m in members)
    ]
    # 副本行一次取全（引擎分组与下方健康灯共用）；引擎行按副本取全量——既做达成写
    # READY 也做份内分组，不再按 instance_id / replica_id 各查一遍
    replica_rows = await deployment_projection.replica_rows_of(db, deployment_id)
    all_engines = await deployment_projection.engine_rows_of(db, [r.replica_id for r in replica_rows])
    ready_ids = set(fully_ready_engines)
    by_replica: dict[str, list] = defaultdict(list)
    for eng in all_engines:
        if eng.instance_id in ready_ids:
            eng.status = EngineStatus.READY
        by_replica[eng.replica_id].append(eng)
    # 份内**全部**引擎 ready ⇔ serving：判定须按副本的全量引擎行，不能只看已就绪
    # 子集——单 full 份两者恰相等，PD 份（prefill+decode 两行）只看子集会让 prefill
    # 未就绪的份被标 serving；空列表的 all() 恒真也一并排除
    replica_by_id = {r.replica_id: r for r in replica_rows}
    for rep_id, engs in by_replica.items():
        if engs and all(e.status == EngineStatus.READY for e in engs):
            rep = replica_by_id.get(rep_id)
            if rep is not None:
                rep.status = ReplicaStatus.SERVING
    # 健康灯按副本现状（存在 down/provisioning 份则 degraded；无副本行旧数据视为健康）
    dep.health_status = deployment_projection.deployment_health_from_replicas(replica_rows)
    await db.flush()


async def record_deployment_crash(db: AsyncSession, deployment_id: str) -> None:
    """运行期实例崩溃投影到部署：health_status→degraded + 崩溃计数/时间落库。

    仅 status=running 期维护——部署达成后实例 CRASHED 才是「运行期崩溃」（creating 期实例
    失败走容器阶段收敛，不计数）。不在此记 deploy_log：实例级崩溃详情已由
    report_instance_status 的 lifecycle 投影写入。FakeSession 兼容：仅 get + 属性写。
    """
    dep = await db.get(ModelDeployment, deployment_id)
    if dep is None or dep.status != DeploymentStatus.RUNNING:
        return
    dep.health_status = HealthStatus.DEGRADED
    dep.last_crash_at = datetime.now(timezone.utc)
    dep.crash_count = (dep.crash_count or 0) + 1
    await db.flush()


async def mark_failed_if_active(
    db: AsyncSession,
    deployment_id: str,
    error: str | None = None,
    message: str | None = None,
    reason: str | None = None,
) -> None:
    """部署失败：仅当 status 仍是 creating/launching/running 才标 failed（不覆盖 stop/delete 终态）。

    running 也在列——运行期崩溃重启耗尽 attempt 走本函数，缺了它部署会永久卡在
    running+STOPPING（controller 不再扫 running，VRAM/assignment 不释放）。失败原因写入
    last_error（截断 1000 字）供 API 暴露；实例同步置 FAILED + last_error（保留 Instance
    行供诊断，删除部署时级联清理）。``message`` 覆盖 deploy_log 行文案（容器阶段失败时
    实例 FAILED 行已带崩溃详情，此处用摘要避免重复；规划/调度失败时缺省仍带完整 error）。

    ``reason``：给用户看的人读原因（如调度拒绝的中文说明）。给了就顶替 ``error`` 落 last_error
    与日志流——``error`` 常是 ``PipelineRejection: no node with ...`` 这类带异常类名的内部
    句式，说给用户听等于没说；排障要的另一份仍在服务端日志里。
    """
    shown = reason or error
    dep = await db.get(ModelDeployment, deployment_id)
    if dep is not None and dep.status in DEPLOYMENT_FAILABLE_STATUSES:
        dep.status = DeploymentStatus.FAILED
        dep.health_status = None  # 运行时健康仅在 running 期有意义，离开即清
        if shown:
            dep.last_error = shown[:1000]
        for iobj in (await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars():
            iobj.status = InstanceStatus.FAILED
            if shown:
                iobj.last_error = shown[:1000]
        # 四层失败态：引擎/副本随容器判死（投影链同规则的批量收敛写点）
        await deployment_projection.mark_all_down(db, deployment_id)
        log_message = message or (f"部署失败：{shown}" if shown else "部署失败")
        # 人读原因顶替了内部 error 时，把后者另附一行 extra，便于对着它排障
        log_extra = {"trigger": "reconciler"}
        if error and error != shown:
            log_extra["error"] = error
        await deploy_log_service.append_log(
            db,
            deployment_id=deployment_id,
            step=DeployStep.ORCHESTRATION,
            # 异常停止/部署失败用 warn 概要；实例级崩溃详情（error）在 report_instance_status 行
            level=LogLevel.WARN,
            status=LogStatus.FAILED,
            message=log_message,
            extra=log_extra,
        )
        await db.flush()


async def update_instance_spec(
    db: AsyncSession, instance_id: str, model_path: str, mmproj_path: str | None = None
) -> None:
    """merge 更新实例 spec 的 model_path（可选 mmproj_path），非替换整体 spec，经 InstanceSpec 校验。

    staging 完成后把 ``node_model.model_path``（Node 上报的权威路径）写回 spec，供容器阶段
    构建启动参数/卷挂载；``mmproj_path``（视觉投影器）同批写回——它按 node_model.local_dir
    重算，权威值只有在下载完成后才知道。merge 语义保住拓扑展开期的其余字段不被覆盖。
    """
    iobj = await db.get(Instance, instance_id)
    if iobj is None:
        return
    merged = {**(iobj.spec or {}), "model_path": model_path}
    if mmproj_path is not None:
        merged["mmproj_path"] = mmproj_path
    iobj.spec = InstanceSpec.model_validate(merged).model_dump()
    await db.flush()


async def delete_assignments(db: AsyncSession, deployment_id: str) -> int:
    """删某部署的 DeploymentGpuAssignment 行（rollback/DELETE 用，保持 VRAM 账实一致）。"""
    result = await db.execute(
        delete(DeploymentGpuAssignment).where(DeploymentGpuAssignment.deployment_id == deployment_id)
    )
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)


async def clear_planning_slate(db: AsyncSession, deployment_id: str) -> None:
    """清某部署的规划产物（容器分片 / 引擎 / 副本 / 卡预留四层行）——重驱动前重建。

    四层必须一起清：``_plan`` 的幂等守卫只看 Instance 行，只清分片会让重建撞
    ``(deployment_id, seq)`` 唯一约束 → 部署永久失败。四层各一条批量 DML，不逐行删。
    """
    replicas = await deployment_projection.replica_rows_of(db, deployment_id)
    replica_ids = [r.replica_id for r in replicas]
    if replica_ids:
        await db.execute(delete(ModelDeploymentInstance).where(ModelDeploymentInstance.replica_id.in_(replica_ids)))
    await db.execute(delete(Instance).where(Instance.deployment_id == deployment_id))
    await db.execute(delete(DeploymentGpuAssignment).where(DeploymentGpuAssignment.deployment_id == deployment_id))
    await db.execute(delete(ModelDeploymentReplica).where(ModelDeploymentReplica.deployment_id == deployment_id))
    await db.flush()


async def delete_endpoints_with_bindings(db: AsyncSession, endpoints: list[ModelEndpoint]) -> None:
    """删端点行及其全部 RouteBinding 成员（先 binding 后 endpoint）。

    悬空绑定是不变量级风险：binding 指着已删端点会使 auto 路由永不清空 → 残留路由
    （/v1/models 仍列、调用 503）。restart 换端点 / finalize 孤儿清理 / 缩容摘份 / 级联
    删部署一律走本函数，不逐处手写。
    """
    ep_ids = [e.id for e in endpoints]
    if not ep_ids:
        return
    await db.execute(delete(RouteBinding).where(RouteBinding.endpoint_id.in_(ep_ids)))
    await db.execute(delete(ModelEndpoint).where(ModelEndpoint.id.in_(ep_ids)))


async def delete_deployment_cascade(db: AsyncSession, deployment_id: str) -> None:
    """级联删 Instance / DeploymentGpuAssignment / ModelEndpoint / DeployLog / ModelDeployment 行
    （无 DB FK，显式删），并清理部署自动发布的 RouteBinding 与空 Route。

    deploy_log 随部署删除一并清除（stop 不走此路径，日志保留用于溯源）。自动发布 route
    （origin=auto）删除部署后其 binding 悬空、route 仍 published（/v1/models 仍列、调用 503）——
    连带删 binding；route 因此变空时删 route（re-deploy 走 ensure_route 重建）。手工建 route
    （origin=manual）即便变空也保留（管理员自建，不随部署删）。
    """
    dep = await db.get(ModelDeployment, deployment_id)

    await deploy_log_service.delete_for_deployment(db, deployment_id)

    # 先清指向本部署端点的 RouteBinding（端点删后 binding 悬空）；收集受影响 route 供末尾判空。
    endpoints = list(
        (await db.execute(select(ModelEndpoint).where(ModelEndpoint.deployment_id == deployment_id))).scalars()
    )
    endpoint_ids = [e.id for e in endpoints]
    affected_route_ids: set[int] = set()
    if endpoint_ids:
        binding_rows = (
            await db.execute(select(RouteBinding.route_id).where(RouteBinding.endpoint_id.in_(endpoint_ids)))
        ).scalars()
        affected_route_ids = set(binding_rows)
        await db.execute(delete(RouteBinding).where(RouteBinding.endpoint_id.in_(endpoint_ids)))

    # 四层规划产物（分片/预留/引擎/副本）走统一的批量清口——删库与重驱动同一套
    # 拆层顺序，避免两处各删一遍将来漂移
    await clear_planning_slate(db, deployment_id)
    for ep in endpoints:
        await db.delete(ep)
    if dep is not None:
        await db.delete(dep)
    await db.flush()

    # 自动发布的 route 因部署删除而空 → 删 route（含租户授权引用；仍绑着别的端点则保留）。
    # 一次 in_ 查询判空，避免逐 route N+1。
    if affected_route_ids:
        still_bound = {
            b.route_id
            for b in (
                await db.execute(select(RouteBinding).where(RouteBinding.route_id.in_(affected_route_ids)))
            ).scalars()
        }
        for route_id in affected_route_ids - still_bound:
            route = await db.get(Route, route_id)
            if route is not None and route.origin == "auto":
                await delete_route(db, route_id=route_id)


async def unpublish_deployment_routes(db: AsyncSession, deployment_id: str) -> None:
    """stop 联动下架：本部署唯一来源的自动发布路由 → unpublished（调用 404 而非 503）。

    stop 不删端点/绑定（restart 复用同一路由），只把「仅由本部署服务」的 auto 路由置
    unpublished；仍被其它端点绑定的路由（共享/其它部署服务）不动；manual 路由不动。
    restart 走 ensure_route + republish_route 恢复 published。
    """
    endpoints = list(
        (await db.execute(select(ModelEndpoint).where(ModelEndpoint.deployment_id == deployment_id))).scalars()
    )
    endpoint_ids = [e.id for e in endpoints]
    if not endpoint_ids:
        return
    affected = {
        b.route_id
        for b in (await db.execute(select(RouteBinding).where(RouteBinding.endpoint_id.in_(endpoint_ids)))).scalars()
    }
    if not affected:
        return
    # 仍被其它端点绑定的路由（其它部署仍服务）→ 排除；Python 过滤（FakeSession 不支持 not_in）
    externally_bound = {
        b.route_id
        for b in (await db.execute(select(RouteBinding).where(RouteBinding.route_id.in_(affected)))).scalars()
        if b.endpoint_id not in endpoint_ids
    }
    for route_id in affected - externally_bound:
        route = await db.get(Route, route_id)
        if route is not None and route.origin == "auto" and route.status == "published":
            route.status = "unpublished"
    await db.flush()


# ===================== 节点共享冲突避免（GPU 占用 status-aware + 端口唯一分配）=====================

# 持有 GPU 声明占用的部署态（stopped/failed/deleting 的残留声明行不计占用）


async def resolve_gpu_occupancy(db: AsyncSession, gpu_uuids) -> dict[str, GpuOccupancy]:
    """解析一批 GPU 的声明占用（status-aware，inspect 回显 / create 校验共用）。

    只聚合 ``DEPLOYMENT_ACTIVE_STATUSES`` 部署的 ``DeploymentGpuAssignment`` 声明显存——声明占用的
    部署已 stop/failed 就放行，不再阻拦用户。返回 uuid → :class:`GpuOccupancy`
    （declared_gb / occupied_pct / occupied_by 归因明细）；无声明的卡不出现在返回里，
    消费方 ``occupancy.get(uuid)`` 为 None 即零占用。容量口径与 ledger 同源
    （``resource_ledger.DEFAULT_GPU_MEMORY_UTILIZATION``）。
    """
    uuids = list(dict.fromkeys(gpu_uuids))
    if not uuids:
        return {}
    totals = {
        g.uuid: (g.total_vram_gb or 0.0) for g in (await db.execute(select(Gpu).where(Gpu.uuid.in_(uuids)))).scalars()
    }
    rows, deps = await _load_assignments_and_deployments(db, uuids)
    return occupancy_from_rows(rows, deps, totals)


def occupancy_from_rows(rows, deps, totals) -> dict[str, GpuOccupancy]:
    # pydantic 模型可变：单循环原地累加（避免中间 tuple/列表拷贝）
    occ: dict[str, GpuOccupancy] = {}
    for r in rows:
        dep = deps.get(r.deployment_id)
        # status 过滤在 Python 侧（FakeSession 兼容，禁复合 where）
        if dep is None or dep.status not in DEPLOYMENT_ACTIVE_STATUSES:
            continue
        e = occ.setdefault(r.gpu_uuid, GpuOccupancy())
        gb = r.allocated_vram_gb or 0.0
        e.declared_gb = round(e.declared_gb + gb, 2)
        e.occupied_by.append(
            OccupiedBy(
                deployment_id=r.deployment_id,
                deployment_name=dep.name,
                declared_gb=gb,
            )
        )
    for uuid, e in occ.items():
        total = totals.get(uuid, 0.0)
        if total > 0:
            e.occupied_pct = round(e.declared_gb / total, 4)
    return occ


async def resolve_card_views(
    db: AsyncSession, gpu_uuids, *, model_id: int | None = None, totals: dict[str, float] | None = None
) -> tuple[dict[str, GpuOccupancy], dict[str, CardSummary]]:
    """一次取行同时产出占用与卡现状两个视图（inspect 一请求内两视图都要）。

    两视图本就是同一批 assignment/部署行：占用只认活跃声明（容量口径），现状认全部
    历史行（痕迹口径 + 挑卡回显）。分开调 :func:`resolve_gpu_occupancy` +
    :func:`resolve_card_status` 会把同一批行查两遍。
    ``totals``（uuid → 总显存）由调用方传入可再省一条 Gpu 查询（inspect 手上已有池）。
    """
    rows, deps = await _load_assignments_and_deployments(db, gpu_uuids)
    if totals is None:
        uuids = list(dict.fromkeys(gpu_uuids))
        totals = {
            g.uuid: (g.total_vram_gb or 0.0)
            for g in (await db.execute(select(Gpu).where(Gpu.uuid.in_(uuids)))).scalars()
        }
    occupancy = occupancy_from_rows(rows, deps, totals)
    status = card_status_from_rows(rows, deps, model_id=model_id)
    return occupancy, status


def _port_band(kind: str) -> int:
    """端口占口带宽：rendezvous 主口带隔离带（NCCL/gloo 会在主口附近开额外通道），
    服务口占单口。登记、避让、本地防撞三处共用，改带宽只动这里。"""
    return RENDEZVOUS_ISOLATION_BAND if kind == "rendezvous" else 1


async def resolve_card_status(db: AsyncSession, gpu_uuids, *, model_id: int | None = None) -> dict[str, CardSummary]:
    """每张卡的现状事实：卡上现有部署（含已停止/失败的）+ 痕迹档位 + 是否同一个模型。

    ``model_id`` 非空时标出「占用的就是同一个模型」（用户挑选调度位置时关心的一条）。**干净卡
    （无任何声明行）不出现在返回里**，消费方按「无事实 = 无占用痕迹」处理；「本地已有该
    模型文件」是**节点**属性，由调用方按需从 ``nodes_with_model`` 补（本函数不查节点）。

    占用（:func:`resolve_gpu_occupancy`，只认活跃）与痕迹（本函数，认全部历史行）是同一批
    数据行的两种视角，查询体共用 :func:`_load_assignments_and_deployments`；差别只在
    「把哪些行算数」。
    """
    rows, deps = await _load_assignments_and_deployments(db, gpu_uuids)
    return card_status_from_rows(rows, deps, model_id=model_id)


def card_status_from_rows(rows, deps, *, model_id: int | None = None) -> dict[str, CardSummary]:
    """由已取的 assignment/部署行组装卡现状视图（与 :func:`resolve_card_status` 同规则）。"""
    facts: dict[str, CardSummary] = {}
    for r in rows:
        dep = deps.get(r.deployment_id)
        active = dep is not None and dep.status in DEPLOYMENT_ACTIVE_STATUSES
        f = facts.setdefault(r.gpu_uuid, CardSummary(uuid=r.gpu_uuid))
        if active:
            f.state = CardState.SHARED
        elif f.state is None:
            f.state = CardState.RESIDUAL
        f.occupants.append(
            CardOccupant(
                deployment_id=r.deployment_id,
                deployment_name=dep.name if dep is not None else "",
                status=dep.status if dep is not None else "unknown",
                active=active,
                same_model=dep is not None and model_id is not None and dep.model_id == model_id,
                declared_gb=r.allocated_vram_gb or 0.0,
            )
        )
    # 卡上现有部署：活跃的排前面（现状比历史更该被看到），同状态按名字稳定排序
    for f in facts.values():
        f.occupants.sort(key=lambda o: (not o.active, o.deployment_name, o.deployment_id))
    return facts


async def placement_prefs(
    db: AsyncSession, *, model_id: int, pool, arch_order=None, card_status: dict[str, CardSummary] | None = None
) -> PlacementPrefs:
    """装配一次调度所用的偏好（建部署 / 扩容 / 预检三处唯一入口）。

    只出**事实**：``arch_order`` 由调用方按适配器能力算好传入（架构知识属适配器，
    service 层不碰）；本地性（哪些节点已有该模型文件）与卡痕迹在这里查库。
    ``card_status``：调用方已查过卡现状（inspect 要回显）时传进来复用，省一次查询。
    """
    if card_status is None:
        card_status = await resolve_card_status(db, [getattr(g, "uuid", "") for g in pool])
    return PlacementPrefs(
        arch_order=tuple(arch_order) if arch_order is not None else None,
        locality_node_ids=frozenset(await nodes_with_model(db, model_id)),
        card_state={uuid: f.state for uuid, f in card_status.items() if f.state is not None},
    )


async def _load_assignments_and_deployments(db: AsyncSession, gpu_uuids):
    """一批 GPU 的声明行 + 其所属部署（占用口径与痕迹口径共用的查询体）。

    返回 ``(assignment 行, {deployment_id: ModelDeployment})``。调用方各自决定哪些行算
    「占用」（只认活跃）哪些算「痕迹」（认全部历史）——查询形状只有这一份。
    """
    uuids = list(dict.fromkeys(gpu_uuids))
    if not uuids:
        return [], {}
    rows = list(
        (await db.execute(select(DeploymentGpuAssignment).where(DeploymentGpuAssignment.gpu_uuid.in_(uuids)))).scalars()
    )
    dep_ids = {r.deployment_id for r in rows}
    deps = (
        {
            d.deployment_id: d
            for d in (
                await db.execute(select(ModelDeployment).where(ModelDeployment.deployment_id.in_(dep_ids)))
            ).scalars()
        }
        if dep_ids
        else {}
    )
    return rows, deps


async def allocate_node_port(db: AsyncSession, node_id: str, kind: str, *, local_taken: set[int] | None = None) -> int:
    """在该节点上分配一个空闲端口（status-aware：非 STOPPED/FAILED 的实例才占端口）。

    kind="service" → 服务口池；"rendezvous" → 多机 rendezvous 池。port/master_port 不是
    Instance 列（在 ``spec`` JSON 里），故按节点取行后 Python 侧解析已占端口，取池内最小
    空闲；池满 → Conflict。同部署内多实例若同节点，由调用方在分配间自行去重。

    rendezvous 主口带 ``RENDEZVOUS_ISOLATION_BAND`` 隔离带：占口按「主口 + 带宽」登记，
    防相邻口被下一个引擎的主口占用（NCCL/gloo 会在主口附近开额外通道）。

    ``local_taken``：本批尚未落库的已占口（调用方跨次调用持有）——DB 里看不到本轮刚分
    配的端口，缺它会让同批第二个实例重复拿到同一个口。整段按 band 参与避让判定，
    与 DB 行的登记口径一致（不会出现「本地只记主口、库里记主口+带」两套语义）。
    """
    band = _port_band(kind)
    pool = SERVICE_PORT_RANGE if kind == "service" else RENDEZVOUS_PORT_RANGE
    key = "port" if kind == "service" else "master_port"
    taken: set[int] = set(local_taken or ())
    for r in (await db.execute(select(Instance).where(Instance.assigned_node == node_id))).scalars():
        if r.status in INSTANCE_PORT_RELEASED_STATUSES:
            continue
        val = (r.spec or {}).get(key)
        if val is not None:
            base = int(val)
            taken.update(range(base, base + band))
    free = next((p for p in pool if all(q not in taken for q in range(p, p + band))), None)
    if free is None:
        raise Conflict(f"节点 {node_id} 的 {kind} 端口池已耗尽（{pool.start}-{pool.stop - 1}）")
    return free


async def allocate_unique_port(
    db: AsyncSession,
    local_taken: dict[tuple[str | None, str], set[int]],
    node_id: str | None,
    kind: str,
) -> int:
    """``allocate_node_port`` + 本地防撞：同一批未落库的实例间不互抢端口。

    ``local_taken`` 由调用方跨次调用持有（键 = (node_id, kind)）——DB 里还看不到本轮
    刚分配的端口（行未落库/批量写回），本地表补上这层去重。登记按 band 展开（与
    ``allocate_node_port`` 的占口口径同源），故本地避让不会把相邻的隔离带口分出去。
    """
    taken = local_taken.setdefault((node_id, kind), set())
    port = await allocate_node_port(db, node_id, kind, local_taken=taken)
    taken.update(range(port, port + _port_band(kind)))
    return port


async def assign_rendezvous_ports(
    db: AsyncSession, instances: list, local_taken: dict[tuple[str | None, str], set[int]]
) -> None:
    """逐引擎实例分配 rendezvous 主口并写回全部分片（多机引擎才需要；就地改 spec）。

    master_addr = 本引擎 head 分片（node_rank==0）所在节点，主口在该节点分配——
    多副本/多引擎落同节点不撞口。与首建（_plan）、重启（restart）、在线扩容（scale）
    共用同一口径。
    """
    by_engine: dict[str, list] = defaultdict(list)
    for inst in instances:
        if getattr(inst, "engine_instance_id", None):
            by_engine.setdefault(inst.engine_instance_id, []).append(inst)
    for shards in by_engine.values():
        if not any(max(1, int(s.nnodes or 1)) > 1 for s in shards):
            continue
        head = next((s for s in shards if s.node_rank == 0 and s.assigned_node), shards[0])
        port = await allocate_unique_port(db, local_taken, head.assigned_node, "rendezvous")
        for shard in shards:
            shard.master_port = port
