"""``ManagerTaskService`` 实现：Node→Manager 任务上报入口。

- ``report_task_event``：append ``task_event`` + 更新 ``task`` 状态/进度/子阶段快照 +
  经 ``TASK_PROJECTORS`` 注册表分发领域投影（node_model、deploy_log 进度点——见
  ``task_projections``）；完成后 ``wake()`` controller 触发 redrive（staging 推进）。
- ``sync_node_state``：node 重连后全量对齐——Manager 认为已 CANCELLED 而 node 仍 active →
  重发 ``cancel_task``。

终态守卫：task 一旦进入 SUCCESS/FAILED/CANCELLED，不再被 stale node 报告覆盖
（状态与 stage 快照同守卫——终态后 stage 冻结为最后快照，保留「死在哪一步」取证价值）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from llmpod.apps.manager.deployment.constants import (
    DEPLOYMENT_TERMINAL_STATUSES,
    DeployStep,
    LogLevel,
    LogStatus,
)
from llmpod.apps.manager.services import task_projections
from llmpod.config.constants import (
    InstanceStatus,
    TASK_INCOMPLETE_STATUSES,
    TASK_TERMINAL_STATUSES,
    TaskStatus,
)
from llmpod.db import get_db_context
from llmpod.models.instance import Instance
from llmpod.services import deploy_log_service, deployment_projection, deployment_service
from llmpod.models.model_deployment import ModelDeployment
from llmpod.models.task import Task
from llmpod.models.task_event import TaskEvent
from llmpod.tunnel.contract import ManagerTaskService
from llmpod.tunnel.schemas import Ack, InstanceStatusPayload, TaskEventPayload, TaskState

log = logging.getLogger("llmpod.apps.manager.services.task_ingest")

# 实例状态迁移 → (deploy_log.step, 步骤进度 status, 缺省 message, 日志 level)：
# report_instance_status 投影用，一个状态一行，避免 step/status/message/level 各表漂移。
# READY 的 step 不在表内——按实例 node_rank 分流到主/工作节点健康检查（见
# report_instance_status），仅取缺省 message。缺省 message 在 node 上报不带 message 时
# 补一句人话（FAILED 通常带崩溃详情，兜底也覆盖空上报）。level：实例不可用事件
# （FAILED/CRASHED）error 级，前端按 level 过滤时崩溃不降权。
_INSTANCE_LIFECYCLE: dict[str, tuple[str, str | None, str | None, str]] = {
    InstanceStatus.STARTING: (DeployStep.START_INSTANCE, LogStatus.RUNNING, "容器启动中", LogLevel.INFO),
    InstanceStatus.RUNNING: (DeployStep.START_INSTANCE, LogStatus.RUNNING, "容器已启动", LogLevel.INFO),
    InstanceStatus.READY: (None, None, "已就绪", LogLevel.INFO),
    InstanceStatus.CRASHED: (DeployStep.RUNNING, LogStatus.FAILED, "容器异常退出，等待自动重启", LogLevel.ERROR),
    InstanceStatus.FAILED: (DeployStep.START_INSTANCE, LogStatus.FAILED, "启动失败", LogLevel.ERROR),
    InstanceStatus.STOPPING: (DeployStep.STOPPING, LogStatus.RUNNING, "正在停止", LogLevel.INFO),
    InstanceStatus.STOPPED: (DeployStep.STOPPING, LogStatus.SUCCESS, "已停止", LogLevel.INFO),
}


class ManagerTaskServiceImpl(ManagerTaskService):
    """Node→Manager 任务上报实现（注册为本地服务 ``ManagerTaskService``）。"""

    def __init__(self, actor_node_id: str | None = None):
        # 生产 RPC 每条已认证通道各持有一个实例；None 只供内部直接调用。
        self.actor_node_id = actor_node_id

    def _check_actor(self, reported_node_id: str, resource_node_id: str | None = None) -> None:
        if self.actor_node_id is not None and (
            reported_node_id != self.actor_node_id
            or (resource_node_id is not None and resource_node_id != self.actor_node_id)
        ):
            raise PermissionError("节点不持有其上报的资源")

    async def report_task_event(self, evt: TaskEventPayload) -> Ack:
        self._check_actor(evt.node_id)
        node_id = self.actor_node_id or evt.node_id
        async with get_db_context() as db:
            # FOR UPDATE：串行化同 task 的并发上报，使「终态不被 stale 报告覆盖」守卫在
            # 真库并发下成立（FakeSession 忽略锁；单 where== 兼容）。
            task = (await db.execute(select(Task).where(Task.id == evt.task_id).with_for_update())).scalar_one_or_none()
            if task is None:
                if self.actor_node_id is not None:
                    raise PermissionError("未知任务，拒绝上报")
                # 内部直调仍保留孤儿事件排障；认证节点不能凭空注入事件。
                db.add(
                    TaskEvent(
                        task_id=evt.task_id,
                        node_id=node_id,
                        status=evt.status,
                        progress=evt.progress,
                        extra=evt.extra,
                        message=evt.message,
                    )
                )
                log.warning("report_task_event: task %s 不存在（孤儿事件，仅记 task_event）", evt.task_id[:8])
                await db.flush()
                return Ack(ok=True, message="orphan event recorded")
            if self.actor_node_id is not None:
                self._check_actor(evt.node_id, task.node_id)
            was_terminal = task.status in TASK_TERMINAL_STATUSES
            prev_status = task.status
            prev_stage = task.stage
            # 状态/阶段是否推进——一个判定两个消费方：TaskEvent 审计行的跳过与 controller
            # 唤醒（见方法尾部）。纯进度滴答（同 status 的 RUNNING、同 stage）不推进：
            # 该表无读方、定位是「状态变更审计」，2s 节拍的字节滴答会把 10 小时下载灌成
            # 上万行；Task 行保留最新进度快照，人读时间线由 deploy_log 投影负责
            status_moved = not was_terminal and (evt.status != prev_status or (evt.stage and evt.stage != prev_stage))
            # append-only 状态变更事件
            if status_moved or was_terminal:
                db.add(
                    TaskEvent(
                        task_id=evt.task_id,
                        node_id=node_id,
                        status=evt.status,
                        progress=evt.progress,
                        extra=evt.extra,
                        message=evt.message,
                    )
                )
            # task 状态推进（终态不被 stale 报告覆盖）
            if not was_terminal:
                task.status = evt.status
                # progress 单调：stale/重复的低进度 RUNNING 报告不得回退
                task.progress = max(task.progress or 0, evt.progress or 0)
                # 子阶段快照：终态守卫同分支——进终态后 stage 冻结
                if evt.stage:
                    task.stage = evt.stage
                if evt.status in TASK_TERMINAL_STATUSES:
                    task.finished_at = datetime.now(timezone.utc)
                elif task.started_at is None and evt.status == TaskStatus.RUNNING:
                    task.started_at = datetime.now(timezone.utc)
                if evt.status == TaskStatus.FAILED and evt.message:
                    task.last_error = evt.message

            # 领域投影（注册表分发；仅「鲜活」事件投影——已终态 task 的 stale 报告不得把
            # present/failed 回退成 downloading）。单个 projector 异常隔离不外传。
            if not was_terminal:
                for projector in task_projections.TASK_PROJECTORS.get(task.resource_type, []):
                    try:
                        await projector(db, task, evt)
                    except Exception as e:
                        log.warning(
                            "projector %s 失败 task=%s: %s",
                            getattr(projector, "__name__", projector),
                            task.id[:8],
                            e,
                        )
            await db.flush()

        # wake controller：只有状态/阶段推进的事件才可能改变收敛结果（staging 就绪、
        # 阶段迁移、终态触发下一 phase）；纯进度滴答不唤醒——2s 节拍的心跳不该把
        # 5s 的 scan_interval 打穿成持续扫描
        if status_moved:
            self._wake_controller()
        return Ack(ok=True)

    async def sync_node_state(self, node_id: str, tasks: list[TaskState]) -> Ack:
        """重连全量对齐。``tasks`` 为 ``list[TaskState]``（wire 上 list[dict]，
        ``coerce_params`` 按注解逐元素归一）。

        三类漂移调和：① Manager=CANCELLED 而 node 仍 active → 重发 cancel；② Manager=RUNNING
        而 node 未上报（node 重启丢任务）→ 回 PENDING 交声明式 redrive；③ node 已报终态而
        Manager 仍未完成（当年终态上报丢失）→ 经 report_task_event 重放终态（带终态守卫
        + 领域投影），否则 reconciler 把 RUNNING 当「继续等」永久卡死部署。
        """
        self._check_actor(node_id)
        node_id = self.actor_node_id or node_id
        states: list[TaskState] = list(tasks or [])
        redispatch: list[tuple[str, str]] = []
        drift_terminal: list[TaskState] = []
        async with get_db_context() as db:
            reported_ids = {st.id for st in states}
            tasks_by_id = {
                row.id: row for row in (await db.execute(select(Task).where(Task.id.in_(reported_ids)))).scalars()
            }
            for st in states:
                task = tasks_by_id.get(st.id)
                if task is None:
                    continue
                self._check_actor(node_id, task.node_id)
                # Manager 已取消而 node 仍认为 active → 重发 cancel（drift 对齐）
                if task.status == TaskStatus.CANCELLED and st.status in TASK_INCOMPLETE_STATUSES:
                    log.info(
                        "sync_node_state: task %s Manager=CANCELLED but node=%s，重发 cancel",
                        st.id[:8],
                        st.status,
                    )
                    redispatch.append((task.node_id, task.id))
                # node 已到终态而 Manager 仍未完成 → 终态上报曾丢失，事务外重放
                elif task.status in TASK_INCOMPLETE_STATUSES and st.status in TASK_TERMINAL_STATUSES:
                    drift_terminal.append(st)
            # node 重启等导致 task 在 node 侧丢失：Manager 仍 RUNNING 的回 PENDING，
            # 由声明式 redrive（_redrive_pending / reconciler 的 PENDING 重发）重新驱动到终态，
            # 否则 reconciler 会把 RUNNING 当「继续等」而永久卡住。
            node_tasks = list((await db.execute(select(Task).where(Task.node_id == node_id))).scalars())
            for t in node_tasks:
                if t.status == TaskStatus.RUNNING and t.id not in reported_ids:
                    t.status = TaskStatus.PENDING
                    log.info(
                        "sync_node_state: task %s Manager=RUNNING 但 node 未上报（疑似重启丢失）→ 回 PENDING",
                        t.id[:8],
                    )
            await db.flush()
        for node_id_, task_id in redispatch:
            await self._redispatch_cancel(node_id_, task_id)
        # 终态漂移重放：node 是执行方，其终态权威；重放走 report_task_event 统一路径
        # （终态守卫 + 进度/stage 快照 + 领域投影），reconciler 见 FAILED 后自然续跑。
        for st in drift_terminal:
            log.info(
                "sync_node_state: task %s Manager 未完成但 node 已报 %s（上报丢失）→ 重放终态",
                st.id[:8],
                st.status,
            )
            await self.report_task_event(
                TaskEventPayload(
                    task_id=st.id,
                    node_id=node_id,
                    status=st.status,
                    progress=st.progress or 0,
                    message=st.last_error,
                    extra=st.extra or {},
                )
            )
        # node 重连：重发该 node 上遗留的 PENDING 任务（dispatch 失败/离线 → 声明式 redrive）
        await self._redrive_pending(node_id)
        return Ack(ok=True)

    async def report_instance_status(self, evt: InstanceStatusPayload) -> Ack:
        """实例长期状态迁移（Node→Manager）：更新 ``instance`` 行 + 唤醒 controller 推进部署。

        守卫：
        - ``STOPPED`` 终态不被任何后续 stale 上报覆盖；
        - 部署已 ``stopped/deleting`` 后拒收 ``STARTING/RUNNING``（防 stale 上报复活实例）；
        - ``CRASHED`` 仅接受实例当前 ∈ {READY, CRASHED}——运行期崩溃只对「曾对外服务」
          的实例成立；STARTING/RUNNING（启动/停止窗口竞态）、FAILED（启动失败走原通道）、
          STOPPING/STOPPED（合法停止）一律拒收，部署已终态（stop/delete 窗口）同样拒收。
          READY → 首次崩溃；CRASHED → monitor TTL 到期重报（仅刷新 last_error，不重复记
          deploy_log）；
        - 孤儿 instance_id（行已随部署删除/从未建）容忍：ack + 日志。
        """
        self._check_actor(evt.node_id)
        async with get_db_context() as db:
            inst = await db.get(Instance, evt.instance_id)
            if inst is None:
                log.warning("report_instance_status: instance %s 不存在（孤儿上报，忽略）", evt.instance_id[:8])
                return Ack(ok=True, message="实例不存在（可能已删除）")
            self._check_actor(evt.node_id, inst.assigned_node)
            # 归属核对只对「上报里带了部署编号」生效：容器被人为删除/消失时节点读不到
            # 容器标签，按契约上报空部署编号，由实例行自身的归属兜底定位（日志投影同此）。
            if self.actor_node_id is not None and (
                inst.assigned_node is None or (evt.deployment_id and evt.deployment_id != inst.deployment_id)
            ):
                raise PermissionError("实例不属于其上报的部署")
            if inst.status == InstanceStatus.STOPPED:
                return Ack(ok=True, message="实例已停止，忽略")
            if evt.status == InstanceStatus.CRASHED:
                if inst.status not in (InstanceStatus.READY, InstanceStatus.CRASHED):
                    log.debug(
                        "report_instance_status: CRASHED 拒收 instance=%s status=%s",
                        evt.instance_id[:8],
                        inst.status,
                    )
                    return Ack(ok=True, message="crashed report rejected")
                # 部署已终态（stop/delete 窗口）拒收：防在途 stale 上报把删除中的实例
                # 写成 CRASHED + 落误导性日志（与 STARTING/RUNNING 分支同守卫）
                if await self._deployment_terminal(db, inst.deployment_id):
                    log.debug(
                        "report_instance_status: CRASHED 拒收（部署已终态）instance=%s",
                        evt.instance_id[:8],
                    )
                    return Ack(ok=True, message="deployment not active, crashed report rejected")
                prev_status = inst.status
                inst.status = InstanceStatus.CRASHED
                if evt.message:
                    inst.last_error = evt.message
                await db.flush()
                # 投影复用主路径逻辑：仅真实迁移（READY→CRASHED）记 deploy_log；
                # CRASHED→CRASHED 重报（monitor TTL 到期）只刷新 last_error，不刷重复行。
                if prev_status != InstanceStatus.CRASHED:
                    await self._project_lifecycle_log(db, inst, evt)
                    # 运行期健康投影：仅真实崩溃迁移才计数/置 degraded（重报不算第二次崩溃）；
                    # record_deployment_crash 内部守卫仅 running 期维护
                    await deployment_service.record_deployment_crash(db, inst.deployment_id)
                # 四层投影链：崩溃后副本判死 → 端点摘流量（幂等；重报也重算无害）
                try:
                    await deployment_projection.project_upwards(db, inst.deployment_id)
                except Exception as e:  # 投影失败不阻断上报主路径
                    log.warning("project_upwards 失败 deployment=%s: %s", inst.deployment_id[:8], e)
                self._wake_controller()
                return Ack(ok=True)
            if evt.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING):
                if await self._deployment_terminal(db, evt.deployment_id):
                    return Ack(ok=True, message="deployment not active, stale report rejected")

            prev_status = inst.status
            inst.status = evt.status
            if evt.container_id:
                inst.container_id = evt.container_id
            if evt.endpoint:
                inst.endpoint = evt.endpoint
            now = datetime.now(timezone.utc)
            if evt.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING):
                if inst.started_at is None:
                    inst.started_at = now
                inst.last_error = None  # 新一轮启动清空历史失败原因
            elif evt.status == InstanceStatus.READY:
                if inst.ready_at is None:
                    inst.ready_at = now
            elif evt.status == InstanceStatus.STOPPED:
                if inst.stopped_at is None:
                    inst.stopped_at = now
            if evt.status == InstanceStatus.FAILED and evt.message:
                inst.last_error = evt.message
            await db.flush()
            # 实例生命周期投影到 deploy_log：仅真实迁移（prev != status）才记，防节点重报刷重复行。
            # 给出「哪个节点-什么步骤-什么状态-什么错」，与编排阶段时间线（set_phase）互补。
            if prev_status != evt.status:
                await self._project_lifecycle_log(db, inst, evt)
            # 四层投影链：容器状态迁移后重算引擎/副本/端点/部署健康（幂等，写点唯一；
            # 无副本行的旧数据也在此回退容器口径——健康灯不再有第二写点）
            try:
                await deployment_projection.project_upwards(db, inst.deployment_id)
            except Exception as e:  # 投影失败不阻断上报主路径
                log.warning("project_upwards 失败 deployment=%s: %s", inst.deployment_id[:8], e)

        self._wake_controller()
        return Ack(ok=True)

    async def _project_lifecycle_log(self, db, inst: Instance, evt: InstanceStatusPayload) -> None:
        """单条实例生命周期 deploy_log（调用方保证 prev != status 已判过）。"""
        lifecycle = _INSTANCE_LIFECYCLE.get(evt.status)
        if evt.status == InstanceStatus.READY:
            # READY 按实例 node_rank 分流到主/工作节点健康检查（head=node_rank==0）。
            # spec 缺 node_rank（旧数据）回退全局 rank（非空列）；脏值兜底当 worker，
            # 别因解析失败丢事件
            nr = (inst.spec or {}).get("node_rank") or inst.rank
            try:
                is_head = int(nr or 0) == 0
            except (TypeError, ValueError):
                is_head = False
            step = DeployStep.HEAD_HEALTH_CHECK if is_head else DeployStep.WORKER_HEALTH_CHECK
            log_status = LogStatus.SUCCESS
        else:
            step = lifecycle[0] if lifecycle else DeployStep.ORCHESTRATION
            log_status = lifecycle[1] if lifecycle else None
        node_id = self.actor_node_id or evt.node_id
        host, ip = await deploy_log_service.node_display(db, node_id)
        # CRASHED 的部署定位用实例行自身 deployment_id（monitor gone 分支上报时该字段为空）
        await deploy_log_service.append_log(
            db,
            deployment_id=evt.deployment_id or inst.deployment_id,
            node_id=node_id,
            step=step,
            level=lifecycle[3] if lifecycle else LogLevel.INFO,
            status=log_status,
            message=evt.message or (lifecycle[2] if lifecycle else None),
            extra={
                "container_id": evt.container_id,
                "instance_id": evt.instance_id,
                "trigger": "system",
                "instance_status": evt.status,
                "node_hostname": host,
                "node_ip": ip,
            },
        )

    # ---- helpers ----

    @staticmethod
    async def _deployment_terminal(db, deployment_id: str) -> bool:
        """部署已终态（stopped/deleting/failed）判定；行缺失（已删）不算终态，交后续守卫。"""
        if not deployment_id:
            return False
        dep = await db.get(ModelDeployment, deployment_id)
        return dep is not None and dep.status in DEPLOYMENT_TERMINAL_STATUSES

    @staticmethod
    def _wake_controller() -> None:
        try:
            from llmpod.apps.manager.deployment.controller import get_controller

            get_controller().wake()
        except Exception:
            pass  # controller 未启动（测试/独立运行）时静默

    @staticmethod
    async def _redispatch_cancel(node_id: str, task_id: str) -> None:
        from llmpod.apps.manager import dispatch

        try:
            await dispatch.cancel_task(node_id, task_id)
        except Exception as e:
            log.debug("sync_node_state: cancel 重发失败 task=%s: %s", task_id[:8], e)

    @staticmethod
    async def _redrive_pending(node_id: str) -> None:
        try:
            from llmpod.apps.manager.deployment.placement import PlacementService

            await PlacementService().redrive_pending(node_id)
        except Exception as e:
            log.debug("sync_node_state: redrive_pending 失败 node=%s: %s", node_id, e)
