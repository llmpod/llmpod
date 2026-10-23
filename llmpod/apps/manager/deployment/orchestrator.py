"""DeploymentOrchestrator：推理部署编排（规划 + 模型分发）。

异步编排，HTTP 立即返回 deployment_id；后台串：
  前置校验 → 拓扑展开 → GPU 调度+VRAM预留 → rank解析 → 持久化 Instance/Assignment
  → STAGING（_stage_models：声明式触发 node 下载 + 轮询 node_model.present，model_path 写回）
  → 交接（phase=PULL_IMAGE）→ reconciler 按 phase 驱动容器阶段 → RUNNING
失败清理 VRAM/assignment；stop/delete 经 cancel 事件 + StopService 收敛。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import PurePosixPath
from collections import defaultdict

from sqlalchemy import select

from llmpod.apps.manager.deployment.category import category_to_path
from llmpod.apps.manager.deployment.engine import get_adapter
from llmpod.apps.manager.deployment.placement import (
    ModelStagingGate,
    canonical_local_dir,
    mmproj_path_for,
)
from llmpod.apps.manager.deployment.schemas import (
    DeploymentConfig,
    GpuAssignment,
    InstanceSpec,
    OccupiedBy,
    PlacementPrefs,
)
from llmpod.apps.manager.deployment.scheduler import (
    GpuScheduler,
    RankResolver,
    validate_cpu_lane,
    validate_same_subnet,
)
from llmpod.apps.manager.model_source.inference import WEIGHT_EXTS
from llmpod.apps.manager.deployment.scheduler.topology import cpu_lane_pins, unit_parallel
from llmpod.apps.manager.deployment.scheduler.gpu import (
    card_reserve,
    infer_selection_arch,
    physical_fits,
    plan_cpu_placement,
)
from llmpod.apps.manager.deployment.resource_ledger import DEFAULT_GPU_MEMORY_UTILIZATION, ResourceLedger
from llmpod.apps.manager.deployment.constants import (
    DEPLOYMENT_RESTARTABLE_STATUSES,
    DeployStep,
    DeploymentPhase,
    DeploymentStatus,
    LogLevel,
    LogStatus,
    EngineStatus,
    ReplicaStatus,
)
from llmpod.config.constants import GpuStatus, InstanceStatus, TaskType
from llmpod.apps.manager.deployment import parallel
from llmpod.apps.manager.deployment import resource
from llmpod.apps.manager import dispatch
from llmpod.config.settings import settings
from llmpod.core.exceptions import Conflict, LLMPodError, LLMPodValidationError
from llmpod.db import get_db_context
from llmpod.models.instance import Instance
from llmpod.models.model import Model
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.model_deployment_instance import ModelDeploymentInstance
from llmpod.models.model_deployment_replica import ModelDeploymentReplica
from llmpod.models.model_endpoint import ModelEndpoint
from llmpod.models.task import Task
from llmpod.services import deploy_log_service, deployment_projection, deployment_service
from llmpod.services.publish_service import ensure_binding, ensure_route, republish_route
from llmpod.tunnel.schemas import DeploySpec
from llmpod.utils.crypto import generate_uuid
from llmpod.utils.docker_naming import (
    LABEL_DEPLOYMENT_ID,
    LABEL_INSTANCE_ID,
    LABEL_MANAGED,
    LABEL_NODE_ID,
    LABEL_ROLE,
    deployment_container_name,
)

log = logging.getLogger("llmpod.apps.manager.deployment.orchestrator")

# STAGING 轮询间隔：deploy 任务等待 node_model 就绪时的 node_model.status 查询节奏。
# attempt 上限（MAX_AUTO_ATTEMPTS）兜底真失败；node 离线则等用户 cancel（_check_cancel）。
STAGING_POLL_INTERVAL = 5.0


async def _all_models_present(db, model_id: int, node_ids: set[str]) -> bool:
    """目标节点是否全部持有 model（node_model.status==present 缓存声明）。

    Manager 侧声明查询（真实下载仍由 placement 走 node 侧 check-existing 复核）；
    空 node_ids（未分配节点）视为已就位，无事可做。与调度本地性偏好（``nodes_with_model``）
    同一读点——两处各写一份会让「谁算已就位」分叉。
    """
    if not node_ids:
        return True
    return node_ids <= await deployment_service.nodes_with_model(db, model_id)


def _build_topology_rows(
    deployment_id: str, instances: list[InstanceSpec]
) -> tuple[list[ModelDeploymentReplica], list[ModelDeploymentInstance]]:
    """由展开产物分组出副本/引擎两层的行（容器分片经 ``spec.engine_instance_id`` 挂回）。

    v1 校验闸（validate_topology）只放行单 full 单元——每份恰好一个引擎实例，分组键
    用 replica_idx 即可；PD 多单元落地时分组键升级为 (replica_idx, 组角色)，引擎行
    的 role 从模板单元抄录。
    """
    by_replica: dict[int, list[InstanceSpec]] = defaultdict(list)
    for inst in instances:
        by_replica[inst.replica_idx].append(inst)
    replicas: list[ModelDeploymentReplica] = []
    engines: list[ModelDeploymentInstance] = []
    for seq in sorted(by_replica):
        shards = by_replica[seq]
        lead = shards[0]
        replica_id = generate_uuid()
        engine_id = generate_uuid()
        for s in shards:
            s.engine_instance_id = engine_id
        replicas.append(
            ModelDeploymentReplica(
                replica_id=replica_id,
                deployment_id=deployment_id,
                seq=seq,
                status=ReplicaStatus.PROVISIONING,
            )
        )
        engines.append(
            ModelDeploymentInstance(
                instance_id=engine_id,
                replica_id=replica_id,
                role="full",
                tp=max(1, int(lead.tp or 1)),
                pp=max(1, int(lead.pp or 1)),
                ep=1,
                nnodes=max(1, int(lead.nnodes or 1)),
                status=EngineStatus.PROVISIONING,
            )
        )
    return replicas, engines


async def _stamp_assigned_instances(
    db,
    instances: list[InstanceSpec],
    *,
    adapter,
    model,
    config,
    gpu_pool,
    arch: str,
    util: float,
    local_taken: dict,
) -> None:
    """assign 之后的逐实例字段收口（_plan 与 _scale_up 共用，规格改动两路同落）。

    model_path/served_model_name/port/task 与生效显存占比落进 InstanceSpec；arch 已随
    assign 按调度到的卡逐实例盖章（异构多副本各份各的镜像），无卡回退部署 arch；目标卡
    型号/驱动（catalog 按 engine+arch+gpu_model+driver 选镜像）按首卡推导。
    """
    task = adapter.task_for(model.model_type)
    canonical_dir = canonical_local_dir(model)
    default_model_path = config.model_path or canonical_dir
    # 视觉投影器（GGUF 多模态）：与主模型同落 canonical 目录（下载保留 repo 相对结构），
    # 故路径 = 该目录 / mmproj 相对路径；用户覆盖 model_path 时按覆盖值的目录算。
    # 仅为**预估**——staging 期由节点回报的 local_dir 覆盖成权威值
    mmproj_base = canonical_dir if not config.model_path else str(PurePosixPath(config.model_path).parent)
    for inst in instances:
        inst.model_path = default_model_path
        inst.model_architecture = model.architecture
        inst.mmproj_path = mmproj_path_for(model, mmproj_base)
        inst.served_model_name = model.name
        inst.port = (
            await deployment_service.allocate_unique_port(db, local_taken, inst.assigned_node, "service")
            if inst.assigned_node
            else adapter.default_port
        )
        inst.task = task
        inst.vram_percent = util
        # 上卡层数两种部署形态都盖章（0=纯 CPU / NGL_ALL=全量上卡）：适配器只读实例值，不再
        # 靠「有卡就发 999」的兜底——那样换「全量」的表示法要同时改 resource 与适配器
        inst.offload_layers = 0 if arch == "cpu" else resource.NGL_ALL
        uuids = inst.assigned_gpu_uuids or []
        tgt = next((g for g in gpu_pool if g.uuid == uuids[0]), None) if uuids else None
        inst.gpu_arch = inst.gpu_arch or arch
        if tgt is not None:
            inst.target_gpu_model = tgt.name
            inst.target_driver_version = tgt.driver_version
    # 多机实例盖章所在节点的网卡名（持有 Node.ip，SOCKET_IFNAME 注入用）：单机不盖（注入
    # 门槛同 nnodes>1，查询也省）；节点未上报（未升级）盖章为 None → 注入回落兜底
    if any(max(1, int(inst.nnodes or 1)) > 1 for inst in instances):
        node_ifnames = await deployment_service.load_node_net_ifnames(
            db, {i.assigned_node for i in instances if i.assigned_node}
        )
        for inst in instances:
            if max(1, int(inst.nnodes or 1)) > 1:
                inst.node_ifname = node_ifnames.get(inst.assigned_node) or None


def _stamp_engine_arch(engines, instances: list[InstanceSpec], fallback_arch: str) -> None:
    """引擎行 arch 随 lead 分片调度到的卡（异构多副本逐份镜像；镜像解析输入随 lead 分片的
    spec 持有，引擎行不另存副本）。"""
    arch_by_engine = {inst.engine_instance_id: inst.gpu_arch for inst in instances if inst.engine_instance_id}
    for eng in engines:
        eng.gpu_arch = arch_by_engine.get(eng.instance_id) or fallback_arch


async def _backfill_legacy_topology_rows(
    db, dep: ModelDeployment, instances: list[InstanceSpec], rows, adapter
) -> None:
    """旧结构数据（四层化迁移前的部署：有容器行、无副本/引擎行）补建两层行。

    投影链查不到份，finalize 的端点计划为空 → 部署永远停在 creating、端点无人重建。
    按实例 spec 的份序号重建 replica/engine 行，容器行的挂载列与 spec 快照一并回填
    （task 键源列优先，重启路径已先清过旧 task，换键无残留）。幂等：已有副本行
    （新结构/补过）直接跳过。

    部署期（``_plan`` 复用存量实例）与 ``restart``（模型在盘时 phase 直落 PULL_IMAGE、
    不再经过 _plan）两个入口都要调——只挂部署期会让老部署重启走到 finalize 时仍无
    副本行可查，端点计划为空、部署停在 finalizing。
    """
    if await deployment_projection.replica_rows_of(db, dep.deployment_id):
        return
    replicas, engines = _build_topology_rows(dep.deployment_id, instances)
    _stamp_engine_arch(engines, instances, adapter.default_arch)
    await deployment_service.persist_topology(db, replicas, engines)
    by_id = {s.instance_id: s for s in instances}
    for row in rows:
        inst = by_id.get(row.instance_id)
        if inst is None:
            continue
        row.model_deployment_instance_id = inst.engine_instance_id
        row.spec = inst.model_dump(mode="json")
    await db.flush()
    log.info(
        "deployment %s: 旧结构数据补建副本/引擎行（%d 份 %d 引擎）",
        dep.deployment_id[:8],
        len(replicas),
        len(engines),
    )


class DeploymentOrchestrator:
    """部署编排器（进程内单例，持有 ResourceLedger）。"""

    def __init__(self) -> None:
        self.ledger = ResourceLedger()
        self._cancel: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # inflight deploy 任务引用（修 fire-and-forget）

    def _cancel_event(self, deployment_id: str) -> asyncio.Event:
        """共享 cancel 事件（request_cancel 可在 deploy 起来前 set，deploy 读同一事件）。"""
        return self._cancel.setdefault(deployment_id, asyncio.Event())

    def _check_cancel(self, deployment_id: str) -> None:
        if self._cancel_event(deployment_id).is_set():
            raise LLMPodError(f"部署 {deployment_id} 已取消")

    # ---- deploy 任务生命周期----

    def start_deploy(self, deployment_id: str) -> asyncio.Task:
        """后台启动 deploy 并跟踪任务引用（替代裸 asyncio.create_task）。"""
        self._cancel_event(deployment_id)  # 事件先于任务存在，避免 request_cancel 竞态
        task = asyncio.create_task(self.deploy(deployment_id), name=f"deploy-{deployment_id[:8]}")
        self._tasks[deployment_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(deployment_id, None))
        return task

    async def await_deploy(self, deployment_id: str, timeout: float = 30.0) -> None:
        """等 deploy 任务结束（delete/stop 用）。超时取消任务。deploy 内部已吞异常。"""
        task = self._tasks.get(deployment_id)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("await_deploy %s timed out (%ss), cancelling", deployment_id[:8], timeout)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # 本函数自己 cancel 后的收尾等待：吞掉（外层未被取消）；外层（stop/
                # delete 路由）自身被取消时透传，否则会假扮成路由异常
                if asyncio.current_task().cancelled():
                    raise
            except Exception:
                pass
        except Exception:
            pass  # deploy 内部已处理异常

    # ---- 主入口 ----

    async def deploy(self, deployment_id: str) -> None:
        """异步部署：规划 + 模型分发，完成即交接（失败自动清理；cancel/stop/delete 不覆盖终态）。

        职责边界：``_plan``（拓扑展开 + GPU 调度 + VRAM 预留 + Instance 持久化，幂等）与
        ``_stage_models``（声明式模型分发至 node present，model_path 写回 instance.spec）。
        staging 完成后 phase=PULL_IMAGE，容器阶段（拉镜像→启动→readiness→finalize）交由
        ``reconciler`` 按 phase 逐步驱动——Manager 重启后可从 phase 续跑，不依赖本协程存活。
        """
        try:
            # 1. 前置：加载 + 展开 + 调度 + 持久化
            async with get_db_context() as db:
                dep, model = await deployment_service.load_deployment(db, deployment_id)
                # DeploymentConfig 在此解析一次，贯穿 _plan / build_run_spec / health 配置
                # （避免 per-instance 重复 model_validate 同一 JSONB）
                config = DeploymentConfig.model_validate(dep.spec or {})
                instances = await self._plan(db, dep, model, config)

            # 清理该部署的残留容器（崩溃恢复/重部署时清孤儿，按 deployment-id label）
            await self.cleanup_deployment_containers(
                dep.deployment_id, {i.assigned_node for i in instances if i.assigned_node}
            )

            # 2. 模型分发：声明式触发下载并等 node_model 就绪——不再 fail-close。
            #    完成时 node_model.model_path（权威）写回 instance.spec + phase=PULL_IMAGE。
            self._check_cancel(deployment_id)
            await self._stage_models(dep, model, instances, deployment_id)

            # 3. 交接容器阶段：reconciler 按 phase 驱动 PULL_IMAGE→START_HEAD→…→RUNNING。
            log.info("deployment %s staged (phase=PULL_IMAGE) — handed off to reconciler", deployment_id)
        except asyncio.CancelledError:
            # stop/delete 超时 cancel（await_deploy → task.cancel）：清理 VRAM 预留即中止。
            # 不 re-raise：deploy 吞自身异常的契约（await_deploy 依赖此）。容器阶段不在本协程，
            # 残留容器由 StopService/converge_stop 收敛。
            log.warning("deployment %s cancelled mid-flight — releasing VRAM", deployment_id[:8])
            await self._handle_deploy_failure(deployment_id, error="已被取消（stop/delete）")
        except Exception as e:
            # 区分用户 stop/delete 触发的 cancel（预期，WARNING 不打 traceback）
            # 与真实失败（ERROR + traceback 供排查）。cancel 由 _check_cancel 抛 LLMPodError，
            # 此时 cancel 事件已 set；真实失败事件未 set。
            if self._cancel.get(deployment_id) is not None and self._cancel[deployment_id].is_set():
                log.warning("deployment %s cancelled (stop/delete) — releasing VRAM", deployment_id[:8])
            else:
                log.exception("deployment %s failed: %s", deployment_id, e)
            await self._handle_deploy_failure(
                deployment_id,
                error=f"{type(e).__name__}: {e}",
                # 异常自带的人读原因（如调度拒绝的中文说明）优先呈现给用户，
                # 上面那条带异常类名的串留给日志排障
                reason=getattr(e, "user_reason", None),
            )
        finally:
            self._cancel.pop(deployment_id, None)

    async def _handle_deploy_failure(
        self, deployment_id: str, error: str | None = None, *, reason: str | None = None
    ) -> None:
        """规划/调度阶段失败的清理：释放 VRAM + 删 assignment + status-aware 标 failed。

        ``reason``：给用户看的人读原因（调度拒绝等自带）；给了就优先于 ``error`` 落库与
        进日志流，``error`` 仍写服务端日志供排障。

        Instance 行保留（FAILED + last_error 供诊断，delete 路由级联清理）；容器阶段不在
        本协程职责内，其容器停止/清理经 StopService 收敛（不双轨）。
        """
        # _plan 阶段失败时 GpuScheduler.assign 已 reserve VRAM → 必须释放，否则预留泄漏到重启
        try:
            await self.ledger.release(deployment_id)
        except Exception as e:
            log.warning("ledger release failed: %s", e)
        # 删 assignment 行 + status-aware 标 failed（同一 session，DB 写入委托 deployment_service）
        async with get_db_context() as db:
            n = await deployment_service.delete_assignments(db, deployment_id)
            await deployment_service.mark_failed_if_active(db, deployment_id, error=error, reason=reason)
        log.info("deployment %s: cleared %d gpu assignment(s)", deployment_id[:8], n)

    # ---- 启动（重启）已停止/失败的部署 ----

    async def restart(self, deployment_id: str, *, force: bool = False, trigger: str = "system") -> None:
        """启动（重启）已停止/失败的部署：在原卡重新预留 VRAM → 重置 → 交控制器重部署。

        与首次 deploy 的区别：复用既有 Instance 规划（assigned_gpu_uuids 已落 spec），不重新调度；
        在原卡上重新预留 VRAM（stop/failed 收敛已释放）。原卡被别的部署占用时：非 force →
        raise Conflict（``details`` 携带 occupied 明细供前端提示）；force → 强制预留（ledger 超额，
        用户承担）仍按原卡重启。模型已在盘、镜像已缓存，故 phase 直设 PULL_IMAGE（staging/pull
        秒过）。旧容器（停止态/崩溃态）由 deploy 开头的 ``cleanup_deployment_containers``（label
        扫除）/``_create_or_replace`` 409-replace 自然替换。

        状态迁移分两步（先短事务置 CREATING 并 commit，再做重预留）：stopped/failed 属于
        controller 的 converge 扫描集合，若在整个 restart 事务窗口内仍是终态，converge 的
        稳态判据（``not ledger.has_reservation``）会被本函数刚写入的预留打破、误入释放分支，
        把预留与 assignment 行清掉——部署实际运行而账本为空，同卡被二次调度。
        """
        # 第一步（短事务）：占住状态——置 CREATING 并 commit，把部署移出 converge 的
        # 终态扫描集合，后续重预留窗口不再与 converge 竞态。预留失败时回滚为原终态。
        async with get_db_context() as db:
            dep, _model = await deployment_service.load_deployment(db, deployment_id)
            if dep.status not in DEPLOYMENT_RESTARTABLE_STATUSES:
                raise Conflict(f"部署当前状态 {dep.status}，无法启动（仅 stopped/failed 可启动）")
            restart_from = dep.status
            dep.status = DeploymentStatus.CREATING
            dep.health_status = None
            dep.last_error = None
            dep.scale_target = None  # 上一轮在途扩缩意图作废（重启是新一轮部署）
            await db.flush()

        try:
            await self._restart_redeploy(deployment_id, restart_from, force=force, trigger=trigger)
        except Exception:
            # 重预留/重置失败：回滚到原终态（保留 last_error 供前端展示原因），并清掉
            # 第一步可能已做的任何账本动作（_reserve_restart 入口的 release 是幂等的，
            # 部分预留残留交给 stopped/failed 的 converge 收敛释放）。
            async with get_db_context() as db:
                dep = await db.get(ModelDeployment, deployment_id)
                if dep is not None and dep.status == DeploymentStatus.CREATING:
                    dep.status = restart_from
                    await db.flush()
            raise

    async def _restart_redeploy(self, deployment_id: str, restart_from: str, *, force: bool, trigger: str) -> None:
        """restart 第二步：实例规划复用下的重预留 + 状态重置 + 交控制器（须已置 CREATING）。"""
        from llmpod.apps.manager.deployment.controller import get_controller

        async with get_db_context() as db:
            dep, model = await deployment_service.load_deployment(db, deployment_id)
            config = DeploymentConfig.model_validate(dep.spec or {})
            irows = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
            if not irows:
                # 规划期失败的部署没有实例行（``_plan`` 在持久化前抛错）——不是「不可启动」，
                # 而是规划从未落盘：清空规划状态后交控制器重跑 _plan（spec 里的 force 原样带过去）。
                # 先释放账本旧预留：重规划会重新记账（累加），残留会让新预留凭空多出一份。
                await self.ledger.release(deployment_id)
                await deployment_service.clear_planning_slate(db, deployment_id)
                await deploy_log_service.append_log(
                    db,
                    deployment_id=deployment_id,
                    step=DeployStep.START,
                    level=LogLevel.WARN,
                    status=LogStatus.RUNNING,
                    message="重新规划部署（上一轮未产出实例）",
                    extra={"trigger": trigger, "force": force, "action": "replan"},
                )
                await db.flush()
                self._cancel.pop(deployment_id, None)
                get_controller().wake()
                log.info("deployment %s restart: no instances → re-planning (force=%s)", deployment_id[:8], force)
                return
            irows.sort(key=lambda r: r.rank)
            instances = [InstanceSpec.model_validate(r.spec or {}) for r in irows]
            adapter = get_adapter(dep.engine)
            # 旧结构数据补建副本/引擎行：restart 不经过 _plan（模型在盘时 phase 直落
            # PULL_IMAGE），不补的话 finalize 查不到份、端点计划为空，部署停在 finalizing
            await _backfill_legacy_topology_rows(db, dep, instances, irows, adapter)

            # 原卡重新预留 VRAM（冲突检测 + force）
            gpu_pool = await deployment_service.load_online_gpus(db)
            await self._reserve_restart(db, deployment_id, instances, gpu_pool, adapter, config, model, force)

            # 端口重分配：_plan 幂等守卫直接复用持久化 spec、不会重跑分配，而停止期间同节点
            # 其他部署可能已拿走旧端口——趁实例仍处 STOPPED/FAILED（不占端口）时重分并写回
            # spec。rendezvous 与 _plan 同口径：逐引擎实例独立分配（head 分片节点查重）。
            _taken: dict[tuple[str | None, str], set[int]] = {}
            for inst in instances:
                if inst.assigned_node:
                    inst.port = await deployment_service.allocate_unique_port(db, _taken, inst.assigned_node, "service")
            await deployment_service.assign_rendezvous_ports(db, instances, _taken)

            # 重置实例：回 PENDING，清上一轮容器态/错误（直接写，不经 set_instance_status 终态守卫）；
            # spec 写回含新端口的快照（irows 按 rank 排序后与 instances 同序）
            for r, inst in zip(irows, instances):
                r.status = InstanceStatus.PENDING
                r.container_id = None
                r.last_error = None
                r.spec = inst.model_dump()
            # 副本/引擎投影回初值：停止期间的 down/serving 是上一轮残影，新一轮容器
            # 阶段推进时投影链重算（此处不重算——分片刚回 PENDING，算了也是 provisioning）
            await deployment_projection.reset_to_provisioning(db, deployment_id)
            # 状态已在 restart 第一步置 CREATING 并 commit；此处维持（幂等写）
            dep.status = DeploymentStatus.CREATING
            dep.health_status = None  # 运行时健康仅在 running 期有意义，重启重新起算
            dep.scale_target = None  # 上一轮在途扩缩意图作废（重启是新一轮部署）
            # 删旧 endpoint 及其绑定成员（重部署 finalize 时重新产出 + 重新绑定）——
            # 悬空成员会使 auto 路由永不清空 → 残留路由
            old_endpoints = list(
                (await db.execute(select(ModelEndpoint).where(ModelEndpoint.deployment_id == deployment_id))).scalars()
            )
            await deployment_service.delete_endpoints_with_bindings(db, old_endpoints)

            # 清旧的 START_INSTANCE / STOP_INSTANCE task：restart 是新一轮部署。START task
            # 必须清——上一轮的 SUCCESS START task 会让 reconciler 幂等短路（prev SUCCESS →
            # 不重起容器）+ 双门补写 READY，导致不建容器却标 RUNNING（假运行部署）。STOP task
            # 一并清：attempt 因唯一索引必须单调，不清会跨轮无限增长；归零后本轮从 1 重计。
            from llmpod.apps.manager.deployment.task_ops import (
                instance_start_key,
                instance_stop_key,
            )

            start_keys = [instance_start_key(r) for r in irows]
            stop_keys = [instance_stop_key(r) for r in irows]
            for t in (
                await db.execute(
                    select(Task).where(Task.resource_key.in_(start_keys), Task.type == TaskType.START_INSTANCE)
                )
            ).scalars():
                await db.delete(t)
            for t in (
                await db.execute(
                    select(Task).where(Task.resource_key.in_(stop_keys), Task.type == TaskType.STOP_INSTANCE)
                )
            ).scalars():
                await db.delete(t)
            await db.flush()
            # phase 判定：模型未全部在盘（staging 失败重启）→ 回 STAGING_MODEL 让 controller 重新分发，
            # 否则直设 PULL_IMAGE（模型已在盘，跳过 staging）。防「下载超限失败 → 重启永远不下载」死循环。
            node_ids = {i.assigned_node for i in instances if i.assigned_node}
            all_present = await _all_models_present(db, model.id, node_ids)
            await deployment_service.set_phase(
                db,
                deployment_id,
                DeploymentPhase.PULL_IMAGE if all_present else DeploymentPhase.STAGING_MODEL,
                trigger=trigger,
            )
            is_start = restart_from == DeploymentStatus.STOPPED
            await deploy_log_service.append_log(
                db,
                deployment_id=deployment_id,
                step=DeployStep.START,
                level=LogLevel.WARN,
                status=LogStatus.RUNNING,
                message="启动部署" if is_start else "重启部署",
                extra={"trigger": trigger, "force": force, "action": "start" if is_start else "restart"},
            )
            await db.flush()

        # 清前次 stop/delete 置过的 cancel 事件，避免 deploy 协程被旧 cancel 误判中止
        self._cancel.pop(deployment_id, None)
        get_controller().wake()
        log.info("deployment %s restart: VRAM re-reserved (force=%s), controller woken", deployment_id[:8], force)

    async def _reserve_restart(
        self, db, deployment_id: str, instances, gpu_pool, adapter, config, model, force: bool
    ) -> None:
        """在 instance.spec 记录的原卡上重新预留 VRAM（先释放本部署旧预留，幂等）。

        - 原卡离线/架构不符 → Conflict；
        - 原卡被别的部署占用且非 force → Conflict（``details.occupied`` 明细）；
          一卡多部署豁免：生效占比下的预留 + 他部署声明占用装得下平台信封（0.9×total）时
          不按冲突处理（显式调低显存占比的多部署同卡 stop→start 不需要 force）；
        - 原卡显存不足且非 force → Conflict；
        - force → 全部 ``force_reserve``（不计容量）。
        预留后 persist_assignments 写回 DeploymentGpuAssignment（VRAM 权威来源）。

        入口先 ``release`` 本部署两个维度（显存 + 内存）的旧预留：stop/failed 收敛路径
        可能没走到释放（纯 CPU 部署无 assignment 行），残留会让下面的累加记账自撞。
        """
        await self.ledger.release(deployment_id)
        vram_per_gpu = adapter.vram_per_gpu(
            model, config.topology, engine_params=config.engine_params, kv_cache=config.kv_cache
        )
        # 异构副本逐份每卡需求（重启预留同 _plan 口径；同形 None 走单值）
        per_replica_vram = adapter.vram_per_gpu_by_copy(
            model, config.topology, engine_params=config.engine_params, kv_cache=config.kv_cache
        )
        # 生效占比：存量实例 spec 已落盘的 vram_percent 优先于 config 重解（口径见
        # effective_memory_utilization）——重启复用存量实例，账本必须与引擎参数同源
        util = adapter.effective_memory_utilization(config.engine_params, [i.vram_percent for i in instances])

        wanted: list[str] = []
        # 逐卡预期 arch：异构多副本各份各的镜像（instance.gpu_arch 已随 assign 逐卡盖章）。
        # 重启按每张原卡自己的 arch 校验，而非首实例 arch——否则 cann 份被误判「架构不符」。
        arch_by_uuid: dict[str, str] = {}
        for inst in instances:
            inst_arch = inst.gpu_arch or adapter.default_arch
            for uuid in inst.assigned_gpu_uuids or []:
                wanted.append(uuid)
                arch_by_uuid[uuid] = inst_arch
        if not wanted:
            # 无卡 = 纯 CPU 部署才走此路。GPU 部署实例缺卡号（旧版本写入的行、从备份恢复的
            # spec）必须拒：否则 delete_assignments 先抹掉显存权威来源（账本 rebuild 也再算不出
            # 这笔占用），实例却仍按 gpu_arch=nvidia 下发 → Node 侧把空卡表翻成 --gpus all，
            # 容器拿整机卡而账本认为卡全空，并发部署会被调度到同一张卡上 OOM。
            stale = [i.instance_id for i in instances if i.gpu_arch != "cpu"]
            if stale:
                raise Conflict(f"部署实例无已分配 GPU（spec 缺 assigned_gpu_uuids），无法在原卡重启：{stale}")
            # 纯 CPU 实例（无卡）：按节点重预留内存（入口已 release 过本部署两维旧预留）
            await deployment_service.delete_assignments(db, deployment_id)
            total_ram = {n.node_id: n.memory_gb for n in await deployment_service.load_online_nodes(db)}
            for inst in instances:
                if not inst.assigned_node or not inst.ram_claim_gb:
                    continue
                if not await self._reserve_ram_or_force(
                    deployment_id,
                    inst.assigned_node,
                    inst.ram_claim_gb,
                    float(total_ram.get(inst.assigned_node, 0.0)),
                    force=force,
                ):
                    raise Conflict(f"节点 {inst.assigned_node} 内存不足，无法在原节点重启（如需继续请使用强制启动）")
            return

        # 原卡仍可用？（在线 + 各自 arch）
        pool_by_uuid = {g.uuid: g for g in gpu_pool}
        offline = [
            u
            for u in wanted
            if u not in pool_by_uuid
            or pool_by_uuid[u].arch != arch_by_uuid[u]
            or pool_by_uuid[u].status != GpuStatus.ONLINE
        ]
        if offline:
            raise Conflict(f"原卡已不可用（离线/架构不符/不存在）：{offline}")

        # 冲突：原卡被别的部署占用（复用 resolve_gpu_occupancy 的 status-aware 解析，
        # 只算 active 部署；顺带拿到 deployment_name 归因）
        occ = await deployment_service.resolve_gpu_occupancy(db, wanted)
        others_by_uuid: dict[str, list[OccupiedBy]] = {}
        for uuid in wanted:
            g = occ.get(uuid)
            if g is None:
                continue
            for o in g.occupied_by:
                if o.deployment_id != deployment_id:
                    others_by_uuid.setdefault(uuid, []).append(o)
        # 一卡多部署豁免：本部署生效占比下的预留 + 他部署声明占用仍装得下平台信封（0.9×total）
        # 时不按冲突处理——显式调低显存占比让多部署共用一张卡是被支持的部署意图。默认整卡
        # 口径下信封必装不下（他部署声明 > 0），排他行为与原「一卡一部署」闸一致；容量
        # 装不下时仍走 force 通道，下方的 ledger/物理闸对豁免路径继续兜底
        colocation_fits = True
        for uuid, entries in others_by_uuid.items():
            card = pool_by_uuid[uuid]
            others_declared = sum(o.declared_gb for o in entries)
            reserve_gb = card_reserve(card, util)
            if reserve_gb + others_declared > card.total_vram_gb * DEFAULT_GPU_MEMORY_UTILIZATION + 1e-6:
                colocation_fits = False
                break
        if others_by_uuid and not force and not colocation_fits:
            exc = Conflict("原卡已被其他部署占用，如需继续请使用强制启动")
            exc.details = {
                "occupied": [
                    {"gpu_uuid": u, "by": [o.model_dump() for o in entries]} for u, entries in others_by_uuid.items()
                ]
            }
            raise exc

        # 删旧 assignment 行（旧预留已在入口 release）
        await deployment_service.delete_assignments(db, deployment_id)

        # 重新预留 + 持久化
        assignments: list[GpuAssignment] = []
        for inst in instances:
            inst_vram = (
                per_replica_vram.get(str(int(inst.replica_idx or 0)), vram_per_gpu)
                if per_replica_vram
                else vram_per_gpu
            )
            allocated: dict[str, float] = {}
            for uuid in inst.assigned_gpu_uuids or []:
                g = pool_by_uuid[uuid]
                reserve_gb = card_reserve(g, util)
                if not force:
                    # 与首建 assign 同一条物理闸：停止期间原卡被平台外进程占走时，
                    # 原卡再预留按账本判会放行，容器起来即 OOM 白失败一轮
                    if not physical_fits(g, reserve_gb, inst_vram):
                        used = getattr(g, "used_vram_gb", None) or 0.0
                        raise Conflict(
                            f"原卡 {uuid} 实时可用显存不足（需 {reserve_gb:.1f}GB，"
                            f"心跳实况 {g.total_vram_gb - used:.1f}GB；可能是平台外进程占用）"
                        )
                if force:
                    await self.ledger.force_reserve(deployment_id, uuid, reserve_gb)
                else:
                    ok = await self.ledger.reserve(deployment_id, uuid, reserve_gb, g.total_vram_gb)
                    if not ok:
                        raise Conflict(
                            f"原卡 {uuid} 显存不足（需 {reserve_gb:.1f}GB，"
                            f"可用 {self.ledger.get_available(uuid, g.total_vram_gb):.1f}GB）"
                        )
                allocated[uuid] = reserve_gb
            assignments.append(
                GpuAssignment(
                    instance_id=inst.instance_id,
                    node_id=inst.assigned_node,
                    gpu_uuids=list(inst.assigned_gpu_uuids or []),
                    vram_gb=inst_vram,
                    gpu_memory_utilization=util,
                    allocated=allocated,
                )
            )
        # resource_type 标签取首实例 arch（异构多副本无法用单值精确表达，与 _plan 同口径）
        await deployment_service.persist_assignments(
            db, assignments, deployment_id, vram_per_gpu, instances[0].gpu_arch or adapter.default_arch
        )

    # ---- 前置：展开 + 调度 + 持久化（DB 读写委托 deployment_service）----

    async def _plan(self, db, dep: ModelDeployment, model: Model, config: DeploymentConfig) -> list[InstanceSpec]:
        adapter = get_adapter(dep.engine)
        # 防御性校验（路由已 422；此处覆盖 controller/直调等绕过路由的路径）
        adapter.validate_category(model.model_type)
        # 并行能力校验（路由已 422；force 部署跳过——引擎最终裁决）
        if not config.force:
            adapter.validate_parallel(config.topology, architecture=model.architecture, model_type=model.model_type)
        # tp 能否切开模型权重：结构类硬约束，不受 force 影响（路由已 422，此处覆盖直调路径）
        parallel.validate_parallel_shape(config.topology, parallel.shape_for(adapter, model))

        # 幂等守卫：部署已有 Instance 行（重启恢复/控制器重驱动再次进入 _plan）→ 直接复用
        # 持久化的规划结果，不重复调度/预留/建行（调度与 VRAM 预留只允许发生一次）。
        existing = list(
            (await db.execute(select(Instance).where(Instance.deployment_id == dep.deployment_id))).scalars()
        )
        if existing:
            instances = [InstanceSpec.model_validate(r.spec or {}) for r in existing]
            instances.sort(key=lambda s: s.rank)
            await _backfill_legacy_topology_rows(db, dep, instances, existing, adapter)
            return instances

        instances = adapter.expand_topology(config.topology)
        for idx, inst in enumerate(instances):
            inst.instance_id = generate_uuid()
            inst.rank = idx  # 全局序号：task resource_key 与 head/worker 区分用（非 rendezvous 语义）

        # 四层挂载：按 replica_idx 分组 → 每份一行 replica + 一行引擎实例，容器分片经
        # spec.engine_instance_id 挂回引擎。v1 校验闸只放行单 full 单元（每份恰一个
        # 引擎）；PD 多单元落地时分组键升级为 (replica_idx, 组角色)
        replicas, engines = _build_topology_rows(dep.deployment_id, instances)

        # GPU 调度 + VRAM 预留（先填 InstanceSpec，再持久化 Instance 带 assigned_node）
        gpu_pool = await deployment_service.load_online_gpus(db)
        # 纯 CPU 部署（allow_cpu 且引擎声明 cpu）：逐层表解出场景与调度结果
        lane: resource.LanePlan | None = None
        lane_nodes: list = []
        # 显式 cpu_only 隐含「允许纯 CPU」（谓词唯一定义处：config.cpu_lane_requested）：
        # 否则该偏好会被静默忽略按存档部署，下面「显式要纯 CPU 却无解」的分支也永远不可达
        if config.cpu_lane_requested and adapter.supports_cpu_lane():
            lane, lane_nodes = await self._resolve_lane(
                db, model, config, adapter, gpu_pool, force=config.force, cluster_id=dep.cluster_id
            )
            if lane is None and config.offload_preference == resource.SCENARIO_CPU_ONLY:
                # 显式要纯 CPU 却无解：报错而不是悄悄改用显存档（部署语义不漂移）。
                # force 部署已在 _resolve_lane 里兜底成超额调度，走到这里只剩「没候选节点」。
                if model.size_gb is None or not model.num_layers:
                    raise LLMPodValidationError(
                        "模型缺架构元数据（num_layers / size_gb），无法测算 CPU 部署资源；请重新探测模型元数据后重试"
                    )
                if not lane_nodes:
                    raise LLMPodValidationError("无在线节点可做纯 CPU 部署")
                raise LLMPodValidationError(
                    "目标节点内存装不下纯 CPU 部署（权重 + KV + 系统保留超过可用内存）："
                    "请更换节点、缩小上下文、改走显存部署，或确认强制部署"
                )
            if lane is not None and lane.scenario == resource.SCENARIO_CPU_ONLY:
                validate_cpu_lane(config.topology)
        vram_per_gpu = adapter.vram_per_gpu(
            model, config.topology, engine_params=config.engine_params, kv_cache=config.kv_cache
        )
        # 异构副本逐份每卡需求（同形 None，assign 走单值口径；各份 tp/pp 不同 → 每卡显存
        # 不同，容量过滤与记账都按各自份的口径）
        per_replica_vram = adapter.vram_per_gpu_by_copy(
            model, config.topology, engine_params=config.engine_params, kv_cache=config.kv_cache
        )
        # 生效显存占比：用户引擎 flag(--gpu-memory-utilization/--mem-fraction-static)，未设则引擎默认
        util = adapter.memory_utilization_from(config.engine_params)
        # 手选卡时按所选卡 arch 推断（选 cann 卡 → cann 部署，解锁 Ascend；否则用默认 arch）
        sel_uuids = config.gpu_selector.gpu_uuids if config.gpu_selector else None
        arch = infer_selection_arch(gpu_pool, sel_uuids, adapter.default_arch)
        # 该架构服务不了本拓扑 → 拒（如手选 Ascend 卡 + pp>1）。自动路径已由下面的
        # schedulable_archs 剔除；手选路径的内核不按 arch 过滤，只能在这里拦。判据与
        # 文案与 create 路由同源（adapter.assert_arch_serves_topology）。
        if sel_uuids:
            adapter.assert_arch_serves_topology(arch, config.topology)

        assignments: list = []
        prefs: PlacementPrefs | None = None
        if lane is not None and lane.scenario == resource.SCENARIO_CPU_ONLY:
            # 纯 CPU：无卡可调，按节点内存 binpack 调度 + RAM 预留（_assign_cpu_lane 内含 KV 记账）
            arch = "cpu"
            await self._assign_cpu_lane(dep.deployment_id, instances, lane, config, adapter, model, lane_nodes)
        else:
            if sel_uuids:
                # 手选卡：调度由用户选集决定、内核走 manual 早退（arch_order/本地性/痕迹全不读），
                # 不查偏好也不算回落序（省池×catalog 扫描与两次 DB 往返）
                prefs = PlacementPrefs()
            else:
                # 自动路径的逐引擎架构回落序（首选架构不够才落后面的架构 → 异构副本）：适配器
                # 声明「本拓扑服务得了 + 池内确有匹配镜像」，与扩容/预检同一入口（schedulable_archs）
                arch_candidates = adapter.schedulable_archs(
                    config.topology,
                    gpu_pool=gpu_pool,
                    engine_version=config.image_tag,
                    explicit_image=config.image,
                )
                # 调度软偏好（本地性 + 卡痕迹）与扩缩容/预检同源装配（placement_prefs）
                prefs = await deployment_service.placement_prefs(
                    db, model_id=model.id, pool=gpu_pool, arch_order=arch_candidates
                )
            assignments = await GpuScheduler(self.ledger).assign(
                dep.deployment_id,
                instances,
                gpu_pool,
                vram_per_gpu,
                arch,
                gpu_selector=config.gpu_selector,
                vram_percent=util,
                prefs=prefs,
                # force 部署跳过容量过滤 + 强制预留（显存不足仍尝试，由引擎/NCCL 兜底）
                force=config.force,
                # 异构副本逐份容量口径（同形 None）
                per_replica_vram=per_replica_vram,
            )
        node_ids = {i.assigned_node for i in instances if i.assigned_node}
        node_ips = await deployment_service.load_node_ips(db, node_ids)
        # 多机（nnodes>1）要求所有节点同子网：NCCL 需双向 L3 可达，跨子网会跑出连不通的假部署。
        # force=True 跳过（启发式，最终由 NCCL rendezvous 兜底）
        if not config.force and any(max(1, int(i.nnodes or 1)) > 1 for i in instances):
            validate_same_subnet(node_ips, settings.multinode_same_subnet_prefix)
        RankResolver.fill(
            instances,
            default_master_port=adapter.default_master_port,
            get_node_ip=lambda nid: node_ips.get(nid, "127.0.0.1"),
        )

        # 端口按节点动态分配（同节点多部署不撞口）：服务口逐实例；rendezvous 逐引擎实例
        # 独立分配。本部署 Instance 行尚未落库，allocator 看不到它们——用本地表防自撞
        # （allocate_unique_port）。+10 口隔离带语义见 allocate_node_port docstring。
        _local_taken: dict[tuple[str | None, str], set[int]] = {}
        await deployment_service.assign_rendezvous_ports(db, instances, _local_taken)

        # 填 model_path / served_model_name / port / task / 生效显存占比 / arch·目标卡
        # 型号驱动（catalog 按 engine+arch+gpu_model+driver 选镜像）——_scale_up 同款收口
        await _stamp_assigned_instances(
            db,
            instances,
            adapter=adapter,
            model=model,
            config=config,
            gpu_pool=gpu_pool,
            arch=arch,
            util=util,
            local_taken=_local_taken,
        )
        _stamp_engine_arch(engines, instances, arch)

        # 持久化四层（replica/engine 先于容器行——容器挂引擎）+ DeploymentGpuAssignment
        # （DB 写入委托 deployment_service）
        await deployment_service.persist_topology(db, replicas, engines)
        await deployment_service.persist_instances(db, dep.deployment_id, instances)
        await deployment_service.persist_assignments(db, assignments, dep.deployment_id, vram_per_gpu, arch)
        await db.flush()
        return instances

    async def _resolve_lane(
        self, db, model, config: DeploymentConfig, adapter, gpu_pool, force: bool = False, cluster_id: int | None = None
    ):
        """解 CPU 部署形态：逐层表在「每节点候选」上求场景与调度结果（取解只在两端，见 resource）。

        候选节点 = 部署集群内的在线节点（``load_online_nodes(cluster_id)``，与 inspect
        的节点池同为集群作用域——不收窄会把副本 binpack 到目标集群之外的节点上），池的
        装配与折让口径全在 :func:`resource.lane_candidates`（显存取该节点引擎可用架构卡
        的单卡最大可用、内存取节点账本可用——两池都是已折过利用率上限的量）。形状/权重
        未知 → 无部署形态（退回纯 GPU 口径）。``pinned`` 只认单数 ``node_id``（逐副本
        指定的 node_ids 不收窄求解域——形态档位逐节点算，调度位置在 _assign_cpu_lane 硬给）。
        ``force=True`` 且显式 ``cpu_only`` 时无解也不拒：改走 :func:`resource.forced_cpu_lane`
        超额调度（用户已确认强制部署，容量交引擎/OOM 兜底）。
        返回 ``(lane, nodes)``——nodes 供内存约束与预留复用，避免二次查库。
        """
        nodes = await deployment_service.load_online_nodes(db, cluster_id)
        # 用户指定了节点（CPU 部署按节点选取 / 手选节点）：只在指定节点上求解
        pinned = config.gpu_selector.node_id if config.gpu_selector else None
        arch_meta = resource.KVArchMeta.from_model(model)
        kv_gb = adapter.kv_gb_for(
            model,
            config.engine_params,
            context_size=config.topology.max_model_len,
            n_parallel=config.kv_cache.n_parallel,
        )
        # 候选池装配与 inspect 共用同一入口（两池都取账本可用量、折让只折一次），引擎
        # 可用架构过滤也在这里——把 cann 卡算进可用量会让部署形态解出 GPU_ONLY，随后 assign
        # 的架构过滤清空候选 → 部署失败，而纯 CPU 部署本可行
        candidates = resource.lane_candidates(
            nodes, gpu_pool, self.ledger, gpu_archs=adapter.offload_gpu_archs(), pinned=pinned
        )
        lane = resource.plan_lane(
            weight_gb=model.size_gb,
            kv_gb=kv_gb,
            total_layers=model.num_layers,
            candidates=candidates,
            allow_cpu=config.allow_cpu,
            preference=config.offload_preference,
            kv_source="formula" if arch_meta is not None else "heuristic",
            # 逐层表的显存侧按该份 tp×pp 均摊（与 estimate_vram_gb 同口径）
            parallel=unit_parallel(config.topology),
        )
        if lane is None and force and config.offload_preference == resource.SCENARIO_CPU_ONLY and candidates:
            lane = resource.forced_cpu_lane(
                weight_gb=model.size_gb,
                kv_gb=kv_gb,
                total_layers=model.num_layers,
                candidates=candidates,
                kv_source="formula" if arch_meta is not None else "heuristic",
            )
        return lane, nodes

    async def _reserve_ram_or_force(self, deployment_id, node_id, ram_gb, total_gb, *, force: bool) -> bool:
        """预留节点内存：容量够正常记账；force 部署超量也调度（与显存 ``force_reserve`` 同口径）。

        账本按量累加（同节点多份各记各的），超额走 ``force_reserve_ram`` 也只加本次——
        调用方需保证同一实例不重复预留（重规划前先 ``release``）。

        容量不足且未 force 时返回 False 而**不抛错**：重启路径报 ``Conflict``、首次调度报
        ``LLMPodValidationError``，异常类型由调用方定，规则只此一份。
        """
        if await self.ledger.reserve_ram(deployment_id, node_id, ram_gb, total_gb):
            return True
        if not force:
            return False
        await self.ledger.force_reserve_ram(deployment_id, node_id, ram_gb)
        return True

    async def _assign_cpu_lane(self, deployment_id, instances, lane, config, adapter, model, nodes) -> None:
        """纯 CPU 档：无卡时选节点（按节点内存 binpack）+ RAM 预留 + 实例字段写回。

        用户指定了节点（``gpu_selector.node_id``，前端 CPU 节点选取即走这条）时只在该节点调度；
        显式 cpu_only 的 ``gpu_selector.node_ids`` 是**逐副本指定**（第 i 份放 node_ids[i]，
        同一节点可指定多份，容量按指定结果逐节点累加，不走 binpack）。
        ``force`` 部署内存不够也调度（超额记账，与显存 force_reserve 同口径）。
        """
        pinned = config.gpu_selector.node_id if config.gpu_selector else None
        # 逐副本指定（cpu_lane_pins 唯一定义处，长度=副本数由 validate_topology 把关）：
        # 指定即分配，容量由下方逐实例 reserve 兜底（同节点多份在账本上累加）
        pinned_nodes = cpu_lane_pins(config.gpu_selector, config.offload_preference)
        if pinned_nodes is not None:
            online = {n.node_id for n in nodes}
            missing = sorted(set(pinned_nodes) - online)
            if missing:
                raise LLMPodValidationError(f"指定节点 {'、'.join(missing)} 不在线，无法做纯 CPU 部署")
            picks = {inst.instance_id: pinned_nodes[i] for i, inst in enumerate(instances)}
        else:
            candidates = [n for n in nodes if n.node_id == pinned] if pinned else nodes
            if not candidates:
                raise LLMPodValidationError(f"指定节点 {pinned} 不在线，无法做纯 CPU 部署")
            picks = plan_cpu_placement(
                instances,
                candidates,
                ram_per_instance_gb=lane.ram_gb,
                ledger=self.ledger,
                force=config.force,
            )
        total_ram = {n.node_id: n.memory_gb for n in nodes}
        for inst in instances:
            node_id = picks[inst.instance_id]
            if not await self._reserve_ram_or_force(
                deployment_id, node_id, lane.ram_gb, float(total_ram.get(node_id, 0.0)), force=config.force
            ):
                raise LLMPodValidationError(f"节点 {node_id} 内存不足（需 {lane.ram_gb:.1f}GB）")
            inst.assigned_node = node_id
            inst.assigned_gpus = []
            inst.assigned_gpu_uuids = []
            inst.gpu_arch = "cpu"
            inst.offload_layers = lane.offload_layers
            inst.ram_claim_gb = lane.ram_gb

    async def cleanup_deployment_containers(self, deployment_id: str, node_ids: set[str]) -> None:
        """清理某部署在节点上的容器（按 deployment-id label：重部署清孤儿 / delete 清在役）。

        正常首次部署无容器（docker_list 返空，no-op）。各节点互不依赖，并发清理（节点内 stop→rm 保序）。
        仅 delete 路由 + 重部署调用——**stop 不删容器**（stop 只停，容器删除是 delete 独有职责）。
        RPC 经 dispatch 门面（出站统一出口）；best-effort：节点离线/单容器失败不阻断其余清理。
        """
        if not node_ids:
            return
        label = f"{LABEL_DEPLOYMENT_ID}={deployment_id}"

        async def _clean_node(node_id: str) -> None:
            try:
                containers = await dispatch.list_containers(node_id, label_filter=[label], all=True)
                for c in containers:
                    name = c.name or ""
                    if not name:
                        continue
                    try:
                        await dispatch.stop_container(node_id, name)
                        await dispatch.remove_container(node_id, name, force=True)
                        log.info("cleanup orphan container %s on %s (deploy %s)", name, node_id, deployment_id[:8])
                    except Exception as e:
                        log.warning("cleanup container %s on %s failed: %s", name, node_id, e)
            except Exception as e:
                log.warning("cleanup docker_list on %s failed (node offline?): %s", node_id, e)

        await asyncio.gather(*[_clean_node(nid) for nid in node_ids])

    async def _stage_models(
        self,
        dep: ModelDeployment,
        model: Model,
        instances: list[InstanceSpec],
        deployment_id: str,
    ) -> None:
        """
        取代旧 ``_stage_check_path`` 的 fail-close：模型不在 node 不再直接失败，而是声明式
        触发下载（``ModelStagingGate`` → ``PlacementService.ensure_present``，idempotent）
        并轮询复核任务终态：

        - 复核任务终态 ``SUCCESS`` 且 ``node_model`` present → 放行，``inst.model_path``
          取 ``node_model.model_path``（权威值）。
        - 复核任务未出终态 / 非 SUCCESS → 继续等（``node_model.status`` 是上次上报的缓存，
          不能代表本次复核结论：上次 present、本次复核发现配套文件缺失时它会短暂仍是 present）。
        - 复核任务失败超自动重试上限 → 终态失败抛 ``LLMPodError``；未超上限则
          ``ensure_present`` 已建下一 attempt 任务，继续等。

        deploy 任务在等待期间保持 inflight（controller ``_active`` 防重入，避免重跑 ``_plan``）；
        cancel 经 ``_check_cancel`` 生效（stop/delete → rollback 释放 VRAM）。
        """
        # node → 该节点上的全部实例（跨机部署一 node 一实例；多 replica 单节点可同 node 多实例）
        nodes_to_insts: dict[str, list[InstanceSpec]] = defaultdict(list)
        for i in instances:
            if i.assigned_node:
                nodes_to_insts[i.assigned_node].append(i)
        pending: set[str] = set(nodes_to_insts)
        log.info("staging %s: 等 model=%s 在 %s 就绪", deployment_id[:8], model.id, sorted(pending))
        async with get_db_context() as db:
            # 模型已在全部目标节点 present → staging 秒过，阶段行提示「已存在跳过下载」而非
            # 误导性的「开始下载模型」；任一节点未就位则正常记录（真的需要下载）
            needs_download = not await _all_models_present(db, model.id, pending)
            await deployment_service.set_phase(
                db,
                deployment_id,
                DeploymentPhase.STAGING_MODEL,
                message=None if needs_download else "模型已存在，跳过下载",
            )

        # 复核任务跨轮保留：同一 node 只派发一次（每轮重派会不断新建任务、永远等不到
        # 同一任务的终态，与 node_model 缓存来回打架）；终态失败才清出，由
        # ensure_present 建下一 attempt。
        gate = ModelStagingGate(model.id)
        while pending:
            self._check_cancel(deployment_id)
            # 1. 未派发节点：声明式触发/续接下载（idempotent）。返回 None 的 not-present node → 超 attempt 上限。
            over_cap = await gate.dispatch(pending)
            # 2. 读复核任务终态 + node_model 推进（select.where + Python 过滤，FakeSession 兼容）
            async with get_db_context() as db:
                confirmed, nm_by_node = await gate.advance(db, pending, over_cap)
            for node_id in sorted(confirmed):
                nm = nm_by_node[node_id]
                node_insts = nodes_to_insts[node_id]
                if nm.model_path:
                    for inst in node_insts:
                        inst.model_path = nm.model_path
                        # 投影器路径按节点回报的目录重算（权威值；_plan 阶段是预估值）
                        inst.mmproj_path = mmproj_path_for(model, nm.local_dir)
                pending.discard(node_id)
                gate.release(node_id)
                log.info(
                    "staging %s: model=%s present on %s → %s",
                    deployment_id[:8],
                    model.id,
                    node_id,
                    node_insts[0].model_path,
                )
            if pending:
                await asyncio.sleep(STAGING_POLL_INTERVAL)

        # 全部就位 → model_path（权威）写回 instance.spec + phase=PULL_IMAGE（交接容器阶段）
        async with get_db_context() as db:
            for inst in instances:
                if inst.model_path:
                    await deployment_service.update_instance_spec(
                        db, inst.instance_id, inst.model_path, inst.mmproj_path
                    )
            # 交接容器阶段：镜像全缓存时由 reconciler PULL_IMAGE 探测到再补「跳过拉取」行
            # （真实答案在探测时已知，此处不做重复预探测）
            await deployment_service.set_phase(db, deployment_id, DeploymentPhase.PULL_IMAGE)
        log.info("staging %s: all models present on %s", deployment_id[:8], sorted(nodes_to_insts))

    # ---- 构建 DeploySpec（runtime 无关）----

    # ---- 状态写回（DB 委托 deployment_service；编排器只持有 cancel/adapter 知识）----

    async def _finalize(
        self,
        db,
        dep: ModelDeployment,
        instances: list[InstanceSpec],
        adapter,
        model: Model,
        *,
        achieved_ids: list[str] | None = None,
    ) -> None:
        """FINALIZING 投影：每副本 × 每协议一条 ModelEndpoint + 部署转 running。

        端点只挂 serving 引擎（role ∈ full/decode；prefill 永不出端点）——api_base 取
        该引擎 head 分片（node_rank=0）的 master_addr:port。幂等对账键 (replica_id,
        protocol)：FINALIZING 崩溃恢复重入补缺行、清不在当前副本集的孤儿行（防残留
        端点进路由）；全齐则跳过重建，仅补路由绑定。已有端点行的展示名按当前副本集
        重算（多/单副本形态切换时改名，见 ``sync_endpoint_names``）。

        ``instances`` 是全量分片（端点寻址要看到存量份的 head）；``achieved_ids`` 是
        **本次达成**的实例集合，缺省 = 全量（首建路径）。在线扩容收尾必须只传新份
        ——否则存量崩溃副本会被一并翻成 READY/serving，健康灯误判、自愈也不再重启它。
        """
        deployment_id = dep.deployment_id
        # finalize 前查 cancel——被 stop/delete 抢先则不覆盖成 running（status 终态守卫在 service.persist_running）
        if self._cancel_event(deployment_id).is_set():
            raise LLMPodError(f"部署 {deployment_id} 在达成确认前被取消")

        protocols = adapter.protocols_for(model.model_type) or [adapter.default_protocol or "openai"]
        replicas = await deployment_projection.replica_rows_of(db, deployment_id)
        replicas.sort(key=lambda r: r.seq)
        engines = await deployment_projection.engine_rows_of(db, [r.replica_id for r in replicas])
        engines_by_replica: dict[str, list] = defaultdict(list)
        for e in engines:
            engines_by_replica[e.replica_id].append(e)
        # 容器分片按引擎分组：head 分片（node_rank=0）出端点地址
        shards_by_engine: dict[str, list[InstanceSpec]] = defaultdict(list)
        for inst in instances:
            if inst.engine_instance_id:
                shards_by_engine.setdefault(inst.engine_instance_id, []).append(inst)

        multi_replica = len(replicas) > 1
        # (replica_id, protocol) → 建行参数；只算「serving 引擎」的副本（prefill-only 的份不出端点）
        plan: dict[tuple[str, str], tuple[str, str]] = {}
        for r in replicas:
            for eng in deployment_projection.serving_engines(engines_by_replica.get(r.replica_id, [])):
                shards = sorted(
                    shards_by_engine.get(eng.instance_id, []),
                    key=lambda s: s.node_rank,
                )
                # 排序后首片即 head（node_rank 最小者）
                head = shards[0] if shards else None
                if head is None:
                    log.warning(
                        "deployment %s replica seq=%s engine %s 无容器分片，跳过端点投影",
                        deployment_id[:8],
                        r.seq,
                        eng.instance_id[:8],
                    )
                    continue
                # TODO(API 网关代理): api_base 用 head 的 master_addr（= 节点 advertise IP）。这隐含
                # 「manager/上层代理能对称访问到 head」的假设。非对称可达拓扑（节点在 manager
                # 够不着的子网里，它能连 manager 但 manager 连不回它）时，单节点部署虽能 running，
                # 但上层转发 /v1/* 请求到 api_endpoint 会失败。v1 不解决：需 API 网关 proxy/隧道（v1.5）
                # 或 overlay 网络把引擎端点暴露成 manager 可达地址。
                # 本地部署 api_base 携带版本根 /v1，path 为不含版本根的子路径（category_to_path 派生）
                api_base = f"http://{head.master_addr or '127.0.0.1'}:{head.port or adapter.default_port}/v1"
                # 展示名取**部署名**（非 model.name）：端点名要当着「上游」标签显示给用户，
                # 而同一模型可以有多份部署——用模型名会让这些端点重名、认不出是哪一份服务。
                name = deployment_projection.endpoint_display_name(dep.name, r.seq, multi_replica=multi_replica)
                for protocol in protocols:
                    plan[(r.replica_id, protocol)] = (api_base, name)

        # 已有端点行的展示名按当前副本集重算（多/单副本形态切换；新建行走下面的 plan）
        existing = await deployment_projection.sync_endpoint_names(db, dep, replicas=replicas)
        existing_keys = {(ep.model_deployment_replica_id, ep.protocol) for ep in existing}
        missing_keys = set(plan) - existing_keys
        # 孤儿清理：不在当前副本集（含改拓扑后的遗留行）不进路由——绑定一并删防悬空成员
        orphans = [ep for ep in existing if (ep.model_deployment_replica_id, ep.protocol) not in plan]

        new_endpoints: list[ModelEndpoint] = []
        for replica_id, protocol in missing_keys:
            api_base, name = plan[(replica_id, protocol)]
            endpoint_path = category_to_path(model.model_type, protocol)
            if endpoint_path is None:
                # category×protocol 无已知 path→该协议行 path=NULL（正常路径下 _plan 已
                # 校验 category，此处仅兜底告警）；API 网关按协议约定 URL 转发
                log.warning(
                    "deployment %s: no endpoint path for category=%r protocol=%r (path=NULL)",
                    deployment_id[:8],
                    model.model_type,
                    protocol,
                )
            new_endpoints.append(
                ModelEndpoint(
                    deployment_id=deployment_id,
                    model_deployment_replica_id=replica_id,
                    engine_type=dep.engine,
                    protocol=protocol,
                    api_base=api_base,
                    path=endpoint_path,
                    served_model=model.name,
                    name=name,
                    operations=adapter.supported_operations,
                    capabilities=list(adapter.supported_features),
                    health_status="healthy",
                )
            )
        if orphans:
            await deployment_service.delete_endpoints_with_bindings(db, orphans)

        if new_endpoints:
            # api_endpoint 代表地址 = 第一份副本（名片字段，权威清单在端点表）
            first_key = (replicas[0].replica_id, protocols[0])
            first_api_base = plan[first_key][0] if first_key in plan else None
            await deployment_service.persist_running(
                db,
                deployment_id,
                achieved_ids if achieved_ids is not None else [i.instance_id for i in instances],
                new_endpoints,
                first_api_base,
            )
        else:
            log.info("deployment %s: ModelEndpoint 已全部建（%d 行），跳过重建", deployment_id[:8], len(existing))

        # 路由查一次、成员逐协议 append（route 复用，避免每端点重复 SELECT）。路由名唯一索引
        # 并发冲突（uq_route_name）不翻部署为 failed——endpoint 已可用，发布缺失可由重入/管理员补。
        try:
            config = DeploymentConfig.model_validate(dep.spec or {})
            pub = config.publish
            if not pub.auto_publish:
                return  # 只部署不发布：端点已建，路由/绑定不建（管理员事后手动发布）
            route = await ensure_route(
                db,
                name=pub.name or model.name,
                context_length=config.topology.max_model_len,
                model_type=pub.model_type or model.model_type,
                description=pub.description,
                tags=pub.tags,
                input_price_per_mtok=pub.input_price_per_mtok,
                output_price_per_mtok=pub.output_price_per_mtok,
                cached_input_price_per_mtok=pub.cached_input_price_per_mtok,
            )
            # stop 下架过的 auto 路由在 restart 时恢复 published（不覆盖管理员改的字段）
            if route.origin == "auto" and route.status == "unpublished":
                await republish_route(db, route_id=route.id)
        except Exception:
            log.exception("deployment auto-publish failed (route=%s)", model.name)
            return
        for ep in existing:
            if ep not in orphans:
                await self._auto_publish_route(db, route, ep.id)
        for ep in new_endpoints:
            await self._auto_publish_route(db, route, ep.id)

    async def _auto_publish_route(self, db, route, endpoint_id: int) -> None:
        """部署即自动发布成员 append（幂等；失败不翻部署为 failed）。

        endpoint 已可用，发布记录（RouteBinding）缺失可由重入/管理员补——绑定失败
        吞掉仅记日志，不阻断部署达成。
        """
        try:
            await ensure_binding(db, route_id=route.id, endpoint_id=endpoint_id)
        except Exception:
            log.exception("deployment auto-publish failed (route=%s)", route.name)

    # ---- 删除守卫 ----

    async def request_cancel(self, deployment_id: str) -> None:
        self._cancel_event(deployment_id).set()

    async def release_vram(self, deployment_id: str) -> None:
        """释放某部署的 VRAM/RAM 预留（删除部署时调；委托 ResourceLedger）。

        DB 权威来源（DeploymentGpuAssignment）已由 ``delete_deployment_cascade`` 删行，
        此处只同步进程内 ResourceLedger 的内存态。暴露为公共方法而非让路由直接触 ``.ledger`` 内部。
        """
        await self.ledger.release(deployment_id)


# 模型卷的挂载点判定见 model_volumes：单文件权重挂所在目录，目录权重整目录挂。
# 「单文件」用 model_source.inference.WEIGHT_EXTS 判——那份清单是权重扩展名的权威词表
# （下载 / 内容哈希 / 布局判定共用），本地再抄一份会在新增格式时漂移。


def model_volumes(model_path: str | None, mmproj_path: str | None = None) -> dict[str, str] | None:
    """模型卷（host:container 同路径）：目录权重直接挂目录，单文件权重挂**所在目录**。

    只挂单文件会让同目录的配套件缺席——GGUF 视觉投影器 ``mmproj-*.gguf`` 与分片都是
    按主模型同目录引用的，容器里看不到就报 ``failed to open GGUF file``。投影器路径单独
    传入时同样成卷：``source_subpath`` 把主模型收窄到子目录时（HF 视觉仓库的常见布局：
    主模型在 ``Q4_K_M/``、mmproj 在仓库根），两者不同目录，只挂主模型目录会把投影器
    关在挂载点之外。
    """
    bases: list[str] = []
    for p in (model_path, mmproj_path):
        if not p:
            continue
        path = PurePosixPath(p)
        # 单文件判定用 model_source 的权重扩展名清单（同一进程内直接复用，不再本地抄一份）
        base = str(path.parent) if path.suffix.lower() in WEIGHT_EXTS else str(path)
        if base not in bases:
            bases.append(base)
    if not bases:
        return None
    # 去掉被其它挂载点覆盖的子路径：父目录已挂进来时再挂一层只是噪音（投影器与主模型
    # 同目录即此情形——两者折算出的挂载点一个是另一个的前缀）
    bases = [b for b in bases if not any(b != o and b.startswith(o.rstrip("/") + "/") for o in bases)]
    return {b: b for b in bases}


def build_run_spec(
    dep: ModelDeployment,
    model: Model,
    inst: InstanceSpec,
    adapter=None,
    config: DeploymentConfig | None = None,
) -> DeploySpec:
    """构建 DeploySpec（runtime 无关；模块级供 orchestrator/reconciler 共用）。

    经 ``DeploySpec`` 命名 kwargs 构造：字段名/类型在 Manager 边界即校验（拼错 kwargs 抛
    TypeError，不会被 Node 侧 ``extra=ignore`` 静默丢弃）——DeploySpec 是 Manager↔Node 的
    唯一定义处。``adapter`` / ``config`` 可省（内部按需解析）；传入则避免重复解析。
    """
    if adapter is None:
        adapter = get_adapter(dep.engine)
    if config is None:
        config = DeploymentConfig.model_validate(dep.spec or {})
    arch = inst.gpu_arch or adapter.default_arch
    # 镜像 + params：config.image 覆盖 > catalog 命中(entry)；catalog 是唯一定义处，miss 即 raise（无默认兜底）
    from llmpod.apps.manager.deployment import image_catalog

    image_ref, entry = image_catalog.resolve_image_ref(
        engine=dep.engine,
        arch=arch,
        gpu_model=getattr(inst, "target_gpu_model", None),
        driver_version=getattr(inst, "target_driver_version", None),
        engine_version=config.image_tag,
        explicit_image=config.image,
    )
    # engine_config 默认值兜底（hf_endpoint/nccl_ifname）在 adapter.build_env 内做（唯一定义处），
    # 此处只透传 config.engine_config 原始值；health_timeout_s 由 health_check 合成传入。
    device = adapter.build_device_config(inst, arch)
    return DeploySpec(
        image=image_ref,
        name=deployment_container_name(dep.deployment_id, inst.instance_id),
        entrypoint=adapter.entrypoint,
        args=adapter.assemble_args(inst, config.topology, entry, config.engine_params),
        env=adapter.assemble_env(
            inst,
            config.topology,
            config.engine_config,
            entry,
            config.engine_params,
            health_timeout_s=config.health_check.timeout_seconds,
        ),
        gpus=device.gpus,
        # 模型路径必须挂进容器（host:container 同路径），否则 vLLM 把本地路径当 repo id 下载 → 崩；
        # 单文件权重挂所在目录，配套件（mmproj/分片）随目录一起进容器
        volumes=model_volumes(inst.model_path, inst.mmproj_path),
        labels={
            LABEL_MANAGED: "true",
            LABEL_DEPLOYMENT_ID: dep.deployment_id,
            LABEL_INSTANCE_ID: inst.instance_id,
            LABEL_NODE_ID: inst.assigned_node,
            LABEL_ROLE: inst.role,
        },
        network_mode=adapter.default_network_mode,
        restart_policy=adapter.default_restart_policy,
        ipc=adapter.default_ipc,
        shm_size=adapter.default_shm_size,
        runtime=device.runtime,
        privileged=device.privileged,
        cap_add=device.cap_add,
        driver_mounts=device.driver_mounts,
        extra_devices=device.extra_devices or None,
    )


# 进程内单例
_orchestrator: DeploymentOrchestrator | None = None


def get_orchestrator() -> DeploymentOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = DeploymentOrchestrator()
    return _orchestrator
