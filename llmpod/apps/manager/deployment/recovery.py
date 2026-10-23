"""Manager 启动恢复：崩溃后状态对齐 + 可重建中间状态清理。

manager 崩溃/重启后：
  - 节点全部标 offline，等 on_hello 重连翻 online（与 mark_gpus_stale 同义）
  - creating 部署中仅规划/调度阶段（phase PENDING/STAGING_MODEL）清空重跑（删副本/引擎/
    分片/卡预留四层行 + phase 重置 PENDING），由 DeploymentController 重驱动重建；
  - 容器阶段（PULL_IMAGE..FINALIZING）的 creating 部署保留 Instance/assignment——
    controller 扫描经 reconciler 从 phase 续跑（Node 未重启：task 继续上报；Node 也重启：
    执行器 reconcile 判定健康容器不重起，否则 FAILED 走 Manager attempt 重发）；
  - stopped/deleting 残留经 controller 兜底收敛；
  - ResourceLedger 从剩余（running 部署的）DeploymentGpuAssignment + Instance.spec 重建。
重驱动时 deploy 起始的 cleanup_deployment_containers 按 deployment-id label 清孤儿容器。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from llmpod.apps.manager.deployment.constants import (
    DEPLOYMENT_INFLIGHT_STATUSES,
    DeploymentPhase,
    STAGING_PHASES,
)
from llmpod.config.constants import GpuStatus, NodeStatus

from llmpod.db import get_db_context
from llmpod.models.node import Node
from llmpod.models.gpu import Gpu
from llmpod.models.model_deployment import ModelDeployment
from llmpod.services import deployment_service

log = logging.getLogger("llmpod.apps.manager.deployment.recovery")


async def recover_on_startup() -> list[str]:
    """启动恢复。返回需重触发的 deployment_id 列表（规划/调度阶段的 creating 部署）。"""
    inflight_ids: list[str] = []
    async with get_db_context() as db:
        # 节点对齐——重启时全部标 offline，等 on_hello 重连翻 online；GPU 同步 stale（原 mark_gpus_stale）
        for n in (await db.execute(select(Node).where(Node.status == NodeStatus.ONLINE))).scalars():
            n.status = NodeStatus.OFFLINE
        for g in (await db.execute(select(Gpu).where(Gpu.status == GpuStatus.ONLINE))).scalars():
            g.status = GpuStatus.STALE
        # creating 部署：仅规划/调度阶段清空重跑（删 instances+assignments，phase 重置 PENDING）；
        # 容器阶段保留行，由 reconciler 从 phase 续跑（不重复调度/预留，不重起健康容器）。
        inflight = list(
            (
                await db.execute(
                    select(ModelDeployment).where(ModelDeployment.status.in_(DEPLOYMENT_INFLIGHT_STATUSES))
                )
            ).scalars()
        )
        for dep in inflight:
            if dep.phase in STAGING_PHASES:
                inflight_ids.append(dep.deployment_id)
                # 四层一起清（副本/引擎/分片/预留）：只清分片会让重驱动重建撞
                # (deployment_id, seq) 唯一约束
                await deployment_service.clear_planning_slate(db, dep.deployment_id)
                dep.phase = DeploymentPhase.PENDING
        await db.flush()
    log.info(
        "startup recovery: %d staging deployment(s) cleared for controller redeploy; "
        "container-phase rows kept for reconciler resume; nodes marked offline",
        len(inflight_ids),
    )
    return inflight_ids
