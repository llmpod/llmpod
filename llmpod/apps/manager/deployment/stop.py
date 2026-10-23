"""StopService：部署容器**停止**的收敛（纯 stop，绝不删容器；delete 路由、reconciler、失败清理共用）。

本服务只负责把容器停掉，**不删除容器**——容器删除是 delete 部署独有职责，由
``orchestrator.cleanup_deployment_containers``（label 扫除）执行，与本服务分离。

停止顺序（幂等可重入）：
1. 取消该部署活跃的 START_INSTANCE task（置 CANCELLED + dispatch cancel），短轮询等其到
   终态——避免 Node readiness 循环与 STOP 竞态把「被停止」误报为「崩溃」；
2. 两阶段反序停止（与启动顺序相反）：先 workers（node_rank>0）后 head（node_rank==0）——
   避免 head 与已死 worker 通信（NCCL 通信器悬挂噪声）；单节点只有 head = 单阶段，
   行为不变；``timeout`` 为**每阶段**预算（多机最坏 2×timeout）；worker 阶段超时
   仍继续 head 阶段（尽力收敛）；
3. 每阶段对非 STOPPED 实例 ensure STOP_INSTANCE task（实例置 STOPPING），Node 执行器
   stop 后报 STOPPED；轮询全部 STOPPED（或 stop task SUCCESS，双门）至超时；
4. 返回是否全部停止。

PULL_IMAGE 为跨部署共享任务（同 node 同镜像一个），停止**不**取消它。
VRAM/assignment 释放与行级联删除由调用方负责（stop/delete/reconcile 各自语义不同）。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from llmpod.apps.manager.deployment import task_ops
from llmpod.apps.manager.deployment.task_ops import (
    instance_start_key as _start_key,
    instance_stop_key as _stop_key,
)
from llmpod.config.constants import (
    TASK_INCOMPLETE_STATUSES,
    TASK_TERMINAL_STATUSES,
    InstanceStatus,
    TaskResourceType,
    TaskStatus,
    TaskType,
)
from llmpod.db import get_db_context
from llmpod.tunnel.schemas import StopInstancePayload
from llmpod.models.instance import Instance
from llmpod.models.task import Task
from llmpod.apps.manager.deployment.task_ops import instance_node_rank as _node_rank
from llmpod.services import deployment_service
from llmpod.utils.docker_naming import deployment_container_name

log = logging.getLogger("llmpod.apps.manager.deployment.stop")

_POLL_INTERVAL_S = 0.5
_START_TERMINAL_WAIT_S = 5.0  # 取消 START task 后等其终态的宽限（防 readiness 竞态误报）
_STOP_TIMEOUT_S = 10.0  # 每阶段（workers / head）停止轮询预算


class StopService:
    """部署停止收敛（无状态；按需实例化）。"""

    async def stop_instances(
        self, deployment_id: str, *, instances: list[Instance] | None = None, timeout: float = _STOP_TIMEOUT_S
    ) -> bool:
        """收敛停止部署的容器（纯 stop，不删容器）。返回是否全部 STOPPED（超时/离线
        返 False，已尽力兜底）。

        两阶段反序：先 workers 后 head（启动顺序的逆序）；timeout 为每阶段预算。
        容器删除（delete 部署）由调用方另行调 ``cleanup_deployment_containers``，不在本服务内。

        ``instances`` 限定作用范围（缺省 = 该部署全部实例）：在线缩容按副本摘份时只停
        被摘份的分片，其余副本的容器不受影响——三件套（cancel START / STOPPING /
        backfill）与全量停止共用同一实现，不另写一套。
        """
        if instances is None:
            async with get_db_context() as db:
                irows = list(
                    (await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars()
                )
            irows.sort(key=lambda r: r.rank)
        else:
            irows = list(instances)
        if not irows:
            return True

        await self._cancel_active_start_tasks(deployment_id, irows)

        # 两阶段反序停止：workers（node_rank>0）先于 head（node_rank==0）
        workers = [r for r in irows if _node_rank(r) > 0]
        heads = [r for r in irows if _node_rank(r) == 0]
        all_stopped = True
        for stage_rows in (workers, heads):
            if not stage_rows:
                continue
            await self._ensure_stop_tasks(deployment_id, stage_rows)
            if not await self._wait_stopped(deployment_id, stage_rows, timeout):
                all_stopped = False
        if all_stopped:
            await self._backfill_stopped(deployment_id, irows)
        return all_stopped

    async def dispatch_stop(self, deployment_id: str) -> None:
        """仅派发停止（cancel 活跃 START + ensure STOP task），不轮询等待。

        失败收敛（reconciler.fail_deployment）用：让 Node 停掉残留容器（含已 SUCCESS 的
        worker），不阻塞 failure 路径。``remove=False``——失败部署保留容器供排查。
        VRAM/assignment 释放由调用方/收敛负责。
        """
        async with get_db_context() as db:
            irows = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
        if not irows:
            return
        await self._cancel_active_start_tasks(deployment_id, irows)
        await self._ensure_stop_tasks(deployment_id, irows)

    async def cancel_start_tasks(self, deployment_id: str, irows: list[Instance]) -> None:
        """只取消在途 START task（不置 STOPPING、不发 STOP）——扩容失败回滚专用。

        回滚路径随即删行放卡：行没了 STOP task 就无处挂靠（完成回调找不到实例行），
        故不走完整 stop_instances。cancel 的执行器路径自身会 stop+rm 容器，足以终结
        「任务在途、容器随后才创建」的竞态窗口——否则账本已释放的卡上会起一个
        无人认领的幽灵容器。
        """
        await self._cancel_active_start_tasks(deployment_id, irows)

    # ---- 步骤 ----

    async def _cancel_active_start_tasks(self, deployment_id: str, irows: list[Instance]) -> None:
        """取消活跃 START task 并短轮询等终态（其执行器 cancel 路径会自行 stop+rm 容器）。"""
        keys = [_start_key(r) for r in irows]
        cancelled: list[Task] = []
        async with get_db_context() as db:
            # 在本 session 内查询并改状态：_tasks_by_keys 返回的是其自身已关闭 session 的
            # detached 对象，跨 session 改属性 + flush 在真库是 no-op（FakeSession 因共享 ref 掩盖）。
            tasks = list((await db.execute(select(Task).where(Task.resource_key.in_(keys)))).scalars())
            for t in tasks:
                if t.status in TASK_INCOMPLETE_STATUSES:
                    t.status = TaskStatus.CANCELLED
                    cancelled.append(t)
            await db.flush()
        for t in cancelled:
            await task_ops.dispatch_cancel_task(t.node_id, t.id)
        if not cancelled:
            return
        # 短轮询：等 node 上报终态（CANCELLED/FAILED/SUCCESS），超时也继续（安全网兜底）
        cancelled_ids = {t.id for t in cancelled}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _START_TERMINAL_WAIT_S
        while loop.time() < deadline:
            rows = await self._tasks_by_keys(keys)
            if all(t.status in TASK_TERMINAL_STATUSES for t in rows if t.id in cancelled_ids):
                return
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _ensure_stop_tasks(self, deployment_id: str, irows: list[Instance]) -> None:
        """对非 STOPPED 实例 ensure STOP_INSTANCE task（实例置 STOPPING + 下发）。

        STOP_INSTANCE 执行器纯 stop（不删容器）；容器删除归 delete 的 cleanup。
        """
        to_dispatch: list[Task] = []
        async with get_db_context() as db:
            for irow in irows:
                if irow.status == InstanceStatus.STOPPED:
                    continue
                rk = _stop_key(irow)
                task, need_dispatch, _denial = await task_ops.ensure_task_row(
                    db,
                    resource_type=TaskResourceType.INSTANCE,
                    resource_key=rk,
                    node_id=irow.assigned_node,
                    task_type=TaskType.STOP_INSTANCE,
                    payload=StopInstancePayload(
                        deployment_id=deployment_id,
                        instance_id=irow.instance_id,
                        rank=irow.rank,
                        container_name=deployment_container_name(deployment_id, irow.instance_id),
                        stop_timeout=10,
                    ).model_dump(),
                )
                if task is None:
                    if _denial == "concurrent":
                        # 并发已建（撞 active 唯一索引）：已在途，不是失败——
                        # 等收敛轮询即可，误报超限会掩盖真实原因
                        continue
                    log.warning(
                        "stop_instances %s: STOP task 超 attempt 上限（rank=%d），容器未停；"
                        "delete 部署时由 label 扫除强制清理",
                        deployment_id[:8],
                        irow.rank,
                    )
                    continue
                if need_dispatch:
                    await deployment_service.set_instance_status(db, irow.instance_id, InstanceStatus.STOPPING)
                    to_dispatch.append(task)
            await db.flush()
        for t in to_dispatch:
            await task_ops.dispatch_task(t)

    async def _wait_stopped(self, deployment_id: str, irows: list[Instance], timeout: float) -> bool:
        """轮询全部实例 STOPPED（或 stop task SUCCESS，双门）至超时。"""
        keys = [_stop_key(r) for r in irows]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            stopped = await self._all_stopped(deployment_id, irows, keys)
            if stopped:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _all_stopped(self, deployment_id: str, irows: list[Instance], keys: list[str]) -> bool:
        async with get_db_context() as db:
            rows = list((await db.execute(select(Instance).where(Instance.deployment_id == deployment_id))).scalars())
        by_id = {r.instance_id: r for r in rows}
        tasks = await self._tasks_by_keys(keys)
        task_ok: dict[str, bool] = {}
        for t in tasks:
            if t.status == TaskStatus.SUCCESS:
                task_ok[t.resource_key] = True
        for irow in irows:
            cur = by_id.get(irow.instance_id)
            if cur is not None and cur.status == InstanceStatus.STOPPED:
                continue
            if task_ok.get(_stop_key(irow)):
                continue  # 双门：stop task 成功（实例上报丢失的兜底）
            return False
        return True

    async def _backfill_stopped(self, deployment_id: str, irows: list[Instance]) -> None:
        """双门补写：stop task SUCCESS 但实例 STOPPED 上报丢失 → 补写 STOPPED。

        与 reconciler WAIT_HEAD_READY 的 READY 补写同一模式——实例状态通道丢失时，
        task 终态是判定的权威依据。
        """
        keys = [_stop_key(r) for r in irows]
        tasks = await self._tasks_by_keys(keys)
        ok_keys = {t.resource_key for t in tasks if t.status == TaskStatus.SUCCESS}
        if not ok_keys:
            return
        async with get_db_context() as db:
            for irow in irows:
                if _stop_key(irow) not in ok_keys:
                    continue
                row = await db.get(Instance, irow.instance_id)
                if row is not None and row.status != InstanceStatus.STOPPED:
                    row.status = InstanceStatus.STOPPED
            await db.flush()

    @staticmethod
    async def _tasks_by_keys(keys: list[str]) -> list[Task]:
        # 与 reconciler._task_rows_by_keys 同款查询；不直接复用是测试桩按本模块命名空间
        # patch get_db_context（集成测试范式），收敛会绕开桩打到真库
        if not keys:
            return []
        async with get_db_context() as db:
            return list((await db.execute(select(Task).where(Task.resource_key.in_(keys)))).scalars())
