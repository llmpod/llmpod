"""总览大屏聚合 service：一次查询组装整屏（统计卡 + 集群 GPU 池 + 部署概览 + 事件流）。

数据源全部复用既有域，不新增存储：metric_rollup（窗口指标，经 metric_query_service
的 summary_core）、Node/Gpu 表（算力面计数与集群归属，各一次查询在 screen 内拉齐）、
Manager 心跳缓冲（GPU 实时，经 node_service.node_gpu_summaries 与节点列表同源）、
model_deployment（部署状态）、audit_log + deploy_log（双源事件流 MERGE）。
本层只做聚合与文本化。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.constants import (
    DeployStep,
    DeploymentStatus,
    LogLevel,
)
from llmpod.config.constants import GpuStatus, NodeStatus
from llmpod.models import AuditLog, Cluster, DeployLog, ModelDeployment
from llmpod.models.gpu import Gpu
from llmpod.models.node import Node
from llmpod.services import metric_query_service
from llmpod.services.node_service import node_gpu_summaries
from llmpod.utils.time import format_datetime, now_local

# 事件流·审计源白名单：资源 → 中文标签，只挑「值得上屏」的资源动作（user/api_key/
# access/settings 等运维噪声不上屏）；配合 action ∈ {create, delete} + result=success
_EVENT_AUDIT_RESOURCES = {
    "node": "节点",
    "cluster": "集群",
    "model": "模型",
    "model_deployment": "部署",
    "publish": "发布",
    "tenant": "租户",
    "endpoint": "推理端点",
}
_EVENT_ACTION_VERBS = {"create": "创建了", "delete": "删除了"}

# 事件流·部署日志源：异常级全收；生命周期步骤收启动/停止/调度编排/提交
_EVENT_LOG_LEVELS = (LogLevel.WARN, LogLevel.ERROR)
_EVENT_LOG_STEPS = (
    DeployStep.START,
    DeployStep.START_INSTANCE,
    DeployStep.STOP,
    DeployStep.STOPPING,
    DeployStep.ORCHESTRATION,
    DeployStep.SUBMIT,
)
_EVENT_LOG_WINDOW = timedelta(hours=24)
_EVENT_LIMIT = 20

# 部署概览计数的状态词表（deleting 为瞬态、launching 为历史遗留，均不展示）
_DEPLOY_COUNT_STATUSES = (
    DeploymentStatus.RUNNING,
    DeploymentStatus.CREATING,
    DeploymentStatus.STOPPED,
    DeploymentStatus.FAILED,
)
_RECENT_DEPLOYMENTS = 5
# 集群池全量上限（单页拉齐；超出说明集群治理另有问题，不在大屏兜）
_MAX_CLUSTERS = 200


async def screen(db: AsyncSession, *, from_: datetime | None = None, to: datetime | None = None) -> dict:
    """组装整屏数据。统计卡指标受时间窗约束（缺省今日 00:00 本地 → 当前），
    算力/部署/事件为实时或近窗值。
    """
    from_, to = metric_query_service.resolve_day_window(from_, to)
    metrics = await metric_query_service.summary_core(db, from_=from_, to=to) or metric_query_service.ZERO_CORE
    panel = await service_panel(db)
    return {**metrics, **panel, "events": await _events(db)}


async def service_panel(db: AsyncSession) -> dict:
    """实时服务状态面板（与指标时间窗无关的部分）：节点/GPU 计数 + 集群池 + 部署概览。

    总览屏（dashboard_overview_service）复用本函数取节点/GPU/集群/部署块，
    不复制查询实现；事件流只有本屏渲染，由 screen() 自行附加（总览屏 30s
    轮询，不为不渲染的面板付 audit/deploy 双源三连查）。
    """
    # 算力面计数走 SQL 聚合（GROUP BY 后行数 = 集群/节点数级，不整表拉 Node 行进内存）：
    # 总/在线节点按集群一条 GROUP BY；GPU 按节点分组（含在线过滤）
    node_rows = (
        await db.execute(
            select(
                Node.cluster_id,
                func.count(Node.id),
                func.count(Node.id).filter(Node.status == NodeStatus.ONLINE),
            ).group_by(Node.cluster_id)
        )
    ).all()
    gpu_rows = (
        await db.execute(
            select(
                Gpu.node_id,
                func.count(Gpu.id),
                func.count(Gpu.id).filter(Gpu.status == GpuStatus.ONLINE),
            ).group_by(Gpu.node_id)
        )
    ).all()

    return {
        "online_nodes": sum(online for _, _, online in node_rows),
        "total_nodes": sum(total for _, total, _ in node_rows),
        "gpu_online": sum(online for _, _, online in gpu_rows),
        "gpu_total": sum(total for _, total, _ in gpu_rows),
        "clusters": await _cluster_pools(db, node_rows, gpu_rows),
        "deployments": await _deploy_counts(db),
        "recent_deployments": await _recent_deployments(db),
    }


async def _cluster_pools(db: AsyncSession, node_rows, gpu_rows) -> list[dict]:
    """集群 GPU 池：静态计数来自 GROUP BY 结果（按集群聚合的节点行 + 按集群再聚合的
    GPU 行），实时均值取 Manager 心跳缓冲（node_gpu_summaries，按卡数加权——
    mean(node) × count == sum(per-GPU)）。

    缓冲不可用（Manager 未初始化/单测）时利用率为 None，计数照常返回。
    """
    clusters = (await db.execute(select(Cluster.id, Cluster.name).order_by(Cluster.id).limit(_MAX_CLUSTERS))).all()
    if not clusters:
        return []

    node_count_by_cluster = {cid: total for cid, total, _ in node_rows}
    online_count_by_cluster = {cid: online for cid, _, online in node_rows}
    gpu_count_by_node = {node_id: total for node_id, total, _ in gpu_rows}

    # 成员/在线成员节点 ID 只取「有 GPU 的节点」粒度（gpu_rows 按节点 GROUP BY 已
    # 过滤掉无 GPU 行）：GPU 计数与实时缓冲查询都不需要无 GPU 节点的 ID。
    # 节点在线态顺带带回（status 列），免第二条在线集查询
    members: dict[int, list[str]] = {}
    online_members: dict[int, list[str]] = {}
    for node_id, cluster_id, status in (
        await db.execute(select(Node.id, Node.cluster_id, Node.status).where(Node.id.in_(gpu_count_by_node)))
    ).all():
        members.setdefault(cluster_id, []).append(node_id)
        if status == NodeStatus.ONLINE:
            online_members.setdefault(cluster_id, []).append(node_id)

    summaries = node_gpu_summaries({nid for ids in online_members.values() for nid in ids})

    pools = []
    for c in clusters:
        online_ids = online_members.get(c.id, [])
        util_weighted = util_base = 0.0
        used_vram = total_vram = 0.0
        for nid in online_ids:
            rt = summaries.get(nid)
            if rt is None:
                continue
            util_weighted += rt["gpu_utilization"] * rt["gpu_count"]
            util_base += rt["gpu_count"]
            used_vram += rt["used_vram_gb"]
            total_vram += rt["total_vram_gb"]
        pools.append(
            {
                "id": c.id,
                "name": c.name,
                "node_count": node_count_by_cluster.get(c.id, 0),
                "online_node_count": online_count_by_cluster.get(c.id, 0),
                "gpu_count": sum(gpu_count_by_node.get(nid, 0) for nid in members.get(c.id, [])),
                "gpu_utilization": round(util_weighted / util_base, 1) if util_base else None,
                "used_vram_gb": round(used_vram, 2) if util_base else None,
                "total_vram_gb": round(total_vram, 2) if util_base else None,
            }
        )
    return pools


async def _deploy_counts(db: AsyncSession) -> dict:
    """部署状态计数（deleting 为瞬态、launching 为历史遗留，均不展示）。"""
    counts = {s: 0 for s in _DEPLOY_COUNT_STATUSES}
    for status, n in (
        await db.execute(
            select(ModelDeployment.status, func.count(ModelDeployment.deployment_id)).group_by(ModelDeployment.status)
        )
    ).all():
        if status in counts:
            counts[status] = n
    return counts


async def _recent_deployments(db: AsyncSession) -> list[dict]:
    """最近部署（按更新时间倒序 top N）。"""
    return [
        {"id": deployment_id, "name": name, "status": status}
        for deployment_id, name, status in (
            await db.execute(
                select(ModelDeployment.deployment_id, ModelDeployment.name, ModelDeployment.status)
                .order_by(ModelDeployment.updated_at.desc())
                .limit(_RECENT_DEPLOYMENTS)
            )
        ).all()
    ]


async def _events(db: AsyncSession) -> list[dict]:
    """双源事件流：审计日志（资源增删）+ 部署日志（异常/生命周期），按时间倒序 MERGE。"""
    audit_items = await _audit_events(db)
    deploy_items = await _deploy_events(db)
    merged = sorted(audit_items + deploy_items, key=lambda x: x[0], reverse=True)
    return [payload for _, payload in merged[:_EVENT_LIMIT]]


async def _audit_events(db: AsyncSession) -> list[tuple[datetime, dict]]:
    rows = (
        await db.execute(
            select(
                AuditLog.actor_id,
                AuditLog.actor_name,
                AuditLog.action,
                AuditLog.resource_type,
                AuditLog.resource_id,
                AuditLog.created_at,
            )
            .where(
                AuditLog.action.in_(_EVENT_ACTION_VERBS),
                AuditLog.resource_type.in_(_EVENT_AUDIT_RESOURCES),
                AuditLog.result == "success",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(_EVENT_LIMIT)
        )
    ).all()

    items: list[tuple[datetime, dict]] = []
    for actor_id, actor_name, action, resource_type, resource_id, created_at in rows:
        who = actor_name or f"用户#{actor_id}"
        target = f" {resource_id}" if resource_id else ""
        items.append(
            (
                created_at,
                {
                    "type": "success" if action == "create" else "error",
                    "text": f"{who} {_EVENT_ACTION_VERBS[action]}{_EVENT_AUDIT_RESOURCES[resource_type]}{target}",
                    "time": format_datetime(created_at),
                },
            )
        )
    return items


async def _deploy_events(db: AsyncSession) -> list[tuple[datetime, dict]]:
    since = now_local() - _EVENT_LOG_WINDOW
    rows = (
        await db.execute(
            select(
                DeployLog.deployment_id,
                DeployLog.level,
                DeployLog.step,
                DeployLog.message,
                DeployLog.created_at,
            )
            .where(
                DeployLog.created_at >= since,
                (DeployLog.level.in_(_EVENT_LOG_LEVELS)) | (DeployLog.step.in_(_EVENT_LOG_STEPS)),
            )
            .order_by(DeployLog.created_at.desc())
            .limit(_EVENT_LIMIT)
        )
    ).all()

    # 部署名上下文（一次性回查，缺名回退短 ID）
    names: dict[str, str] = {}
    dep_ids = {r.deployment_id for r in rows}
    if dep_ids:
        names = {
            deployment_id: name
            for deployment_id, name in (
                await db.execute(
                    select(ModelDeployment.deployment_id, ModelDeployment.name).where(
                        ModelDeployment.deployment_id.in_(dep_ids)
                    )
                )
            ).all()
        }

    items: list[tuple[datetime, dict]] = []
    for r in rows:
        name = names.get(r.deployment_id) or r.deployment_id[:8]
        type_ = {LogLevel.WARN: "warning", LogLevel.ERROR: "error"}.get(r.level, "info")
        message = r.message or r.step
        items.append(
            (
                r.created_at,
                {
                    "type": type_,
                    "text": f"[{name}] {message}",
                    "time": format_datetime(r.created_at),
                },
            )
        )
    return items
