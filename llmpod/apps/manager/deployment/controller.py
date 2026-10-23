"""DeploymentController：部署触发的后台控制循环。

desired state（``ModelDeployment.status``/``phase``）写库后，常驻循环收敛 actual→desired：
- creating + staging 阶段（PENDING/STAGING_MODEL）→ ``orchestrator.start_deploy`` 驱动规划+调度协程；
- creating + 容器阶段（PULL_IMAGE..FINALIZING）→ ``reconciler.advance_deployment`` 推进一步；
- stopped/deleting 残留 → 崩溃兜底收敛（路由内已同步停止过一次）；
- running + phase=SCALING → 在线扩缩驱动（路由只写 desired state：意图 + SCALING）；
- running + 任一实例非 READY → ``reconciler_runtime_instances`` 运行期自愈（崩溃重启）；
- ``_active`` 字典登记驱动中的部署（key=deployment_id，value=Task），兼作防重入集合；
- ``wake()`` 让写库后近实时触发（task/instance 上报也会 wake，容器阶段推进不必等 scan_interval）；
- 首扫前等 ``startup_grace``（manager 重启后等节点 ``on_hello`` 重连）。

进程内任务崩 / 孤儿态循环下次扫描自愈（每个驱动函数自身幂等）。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from llmpod.apps.manager.deployment import reconciler
from llmpod.apps.manager.deployment.orchestrator import get_orchestrator
from llmpod.apps.manager.deployment.constants import (
    DEPLOYMENT_RECONCILE_STATUSES,
    DEPLOYMENT_TERMINAL_STATUSES,
    RUNTIME_ABNORMAL_INSTANCE_STATUSES,
    DeploymentPhase,
    DeploymentStatus,
)
from llmpod.db import get_db_context
from llmpod.models.instance import Instance
from llmpod.models.model_deployment import ModelDeployment

log = logging.getLogger("llmpod.apps.manager.deployment.controller")

_SCAN_INTERVAL_S = 5.0
_STARTUP_GRACE_S = 20.0  # 重启后等节点重连（承接原 schedule_redeploy 的 grace 语义）


class DeploymentController:
    """部署控制循环（进程内单例，由 ManagerLifespan spawn/cancel）。"""

    def __init__(self, scan_interval: float = _SCAN_INTERVAL_S, startup_grace: float = _STARTUP_GRACE_S):
        self._scan_interval = scan_interval
        self._startup_grace = startup_grace
        self._wake = asyncio.Event()
        # 正在被驱动（start_deploy/advance/scale/自愈）的 deployment_id → 其 Task；
        # 兼作防重入集合，退出循环前统一取消并 join
        self._active: dict[str, asyncio.Task] = {}

    def wake(self) -> None:
        """新写库后唤醒（近实时扫描，免等 scan_interval）。"""
        self._wake.set()

    async def run(self) -> None:
        """常驻控制循环。由 ManagerLifespan.startup spawn、shutdown cancel。"""
        # 单例跨 lifespan 复用时，先清理上次遗留的子任务，再重建去重状态。
        await self._stop_active()
        try:
            # 首扫前等节点重连（可被 wake 中断：grace 期间新建的部署不延迟）
            if self._startup_grace > 0:
                log.info("controller: %.0fs startup grace (等节点重连，可被 wake 中断)", self._startup_grace)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._startup_grace)
                except asyncio.TimeoutError:
                    pass
            log.info("controller: control loop started (scan_interval=%.1fs)", self._scan_interval)
            while True:
                self._wake.clear()  # clear-before-scan，scan 期间到来的 wake 不被吞
                try:
                    await self._scan_once()
                except Exception:
                    log.exception("controller scan error")
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._scan_interval)
                except asyncio.TimeoutError:
                    pass  # 超时即进入下一轮扫描
        finally:
            await self._stop_active()

    async def _stop_active(self) -> None:
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    async def _scan_once(self) -> None:
        """单轮扫描：creating 部署按 phase 分流驱动；stopped/deleting 兜底收敛；running 自愈 + 扩缩续跑。"""
        orch = get_orchestrator()
        async with get_db_context() as db:
            deps = list(
                (
                    await db.execute(
                        select(ModelDeployment).where(ModelDeployment.status.in_(DEPLOYMENT_RECONCILE_STATUSES))
                    )
                ).scalars()
            )

            # 扩缩续跑只看 phase 子态：轻查列（不水合 spec JSONB）。单条件查询
            # （终态部署不会带 SCALING，status 过滤由 drive_scaling 内部守卫兜）
            scaling_dids = list(
                (
                    await db.execute(
                        select(ModelDeployment.deployment_id).where(ModelDeployment.phase == DeploymentPhase.SCALING)
                    )
                ).scalars()
            )
            # 运行期自愈：先查异常实例（稳态空集，走 idx_instance_status），有命中才与
            # RUNNING 部署做交。健康集群每轮只花一条空结果查询，不水合整行
            abnormal = list(
                (
                    await db.execute(select(Instance).where(Instance.status.in_(RUNTIME_ABNORMAL_INSTANCE_STATUSES)))
                ).scalars()
            )
            runtime_dids: set[str] = set()
            if abnormal:
                running_ids = set(
                    (
                        await db.execute(
                            select(ModelDeployment.deployment_id).where(
                                ModelDeployment.status == DeploymentStatus.RUNNING
                            )
                        )
                    ).scalars()
                )
                runtime_dids = {r.deployment_id for r in abnormal if r.deployment_id in running_ids}
        for dep in deps:
            did = dep.deployment_id
            if did in self._active:
                continue  # 防重入
            if dep.status == DeploymentStatus.CREATING:
                if dep.phase in reconciler.STAGING_PHASES:
                    # 规划+调度协程（返回 Task 而非协程，形状与 _launch 不同；先拿 task
                    # 成功才登记，同步抛错则下次重试）
                    try:
                        task = orch.start_deploy(did)
                    except Exception:
                        log.exception("controller: start_deploy failed for %s (will retry next scan)", did[:8])
                        continue
                    self._active[did] = task
                    log.info("controller: driving deploy %s (phase=%s)", did[:8], dep.phase)
                    task.add_done_callback(self._track(did))
                elif dep.phase in reconciler.CONTAINER_PHASES:
                    # 容器阶段：reconciler 每轮推进一步（幂等）
                    self._launch(did, "advance", reconciler.advance_deployment(did))
            elif dep.status in DEPLOYMENT_TERMINAL_STATUSES:
                # 停止兜底：路由内已同步停止过一次；此处收敛 Manager 崩溃/路由超时后的残留
                # failed：fail_deployment 只派发了停止、未释放 VRAM；待实例全 STOPPED 后由此释放
                self._launch(did, "converge", self._converge_stop(dep))

        # 在线扩缩驱动（进程内唯一执行者：路由只写 desired state，此处据意图推进；
        # 幂等——从「哪层行已建」起续，scale 内部自判进度）
        from llmpod.apps.manager.deployment.scale import drive_scaling

        for did in scaling_dids:
            if did not in self._active:
                self._launch(did, "scale", drive_scaling(did))

        # RUNNING 部署的运行期自愈：崩溃实例自动重启（reconcile_runtime_instances 自身
        # 幂等 + 双守卫，非 RUNNING / 停止竞态时 no-op）。
        #
        # TODO(探测健康)：当前对「部署是否健康」是**推论**而非**探测**——唯一信号是
        # Instance.status ∈ RUNTIME_ABNORMAL（由 Node monitor 容器 inspect 上报 CRASHED
        # 驱动）。node 掉线时 monitor 无法上报，Manager 只能假定容器还活着，部署一直显示
        # running 直到用户 stop/delete。未来应加端点级主动探测——周期对 ModelEndpoint
        # 逐行 HTTP GET api_base/health（权威清单在端点表，不依赖部署单值 api_endpoint
        # 代表地址），探测失败同样进入本恢复循环。
        for did in runtime_dids - self._active.keys():
            self._launch(did, "runtime", reconciler.reconcile_runtime_instances(did))

    def _launch(self, did: str, label: str, coro) -> None:
        """spawn 一个驱动协程并登记（done 回调释放槽 + 取异常）。"""
        try:
            task = asyncio.create_task(coro, name=f"{label}-{did[:8]}")
        except Exception:
            coro.close()  # 未入队协程须手动关闭，防「never awaited」告警
            log.exception("controller: %s launch failed for %s (will retry next scan)", label, did[:8])
            return
        self._active[did] = task
        task.add_done_callback(self._track(did))

    def _track(self, did: str):
        """done_callback：释放驱动槽 + 取回后台任务异常。

        ``asyncio.create_task`` 的协程自身异常落在 Task 对象上，默认仅在 GC 时告警，会被静默
        吞掉（部署无声卡在当前 phase）。这里显式取回并记日志，让 advance/start_deploy/converge
        的抛错可见——下一轮 scan 会重试（各驱动函数自身幂等）。

        身份校验（``is t``）防跨生命周期陈旧回调误删新任务：单例 controller 被下一任
        lifespan 复用时，上一生命周期遗留的 done 回调可能晚于 ``_stop_active`` 才触发。
        """

        def _cb(t):
            if self._active.get(did) is t:
                self._active.pop(did, None)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    log.error("controller: 部署 %s 后台推进任务抛错（下轮 scan 重试）: %r", did[:8], exc)

        return _cb

    @staticmethod
    async def _converge_stop(dep: ModelDeployment) -> None:
        """stopped/deleting 残留收敛：与 stop/delete 路由同一 StopService（幂等兜底）。"""
        try:
            await reconciler.converge_stop_deployment(dep.deployment_id)
        except Exception:
            log.exception("controller: converge_stop failed for %s", dep.deployment_id[:8])


_controller: DeploymentController | None = None


def get_controller() -> DeploymentController:
    """进程内单例。"""
    global _controller
    if _controller is None:
        _controller = DeploymentController()
    return _controller
