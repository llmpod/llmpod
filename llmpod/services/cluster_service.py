"""集群管理服务。"""

from __future__ import annotations

import logging

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.core.exceptions import Conflict
from llmpod.config.constants import NodeStatus

from llmpod.models.cluster import Cluster
from llmpod.models.gpu import Gpu
from llmpod.models.node import Node

log = logging.getLogger("llmpod.services.cluster")


async def create_cluster(
    db: AsyncSession,
    name: str,
    cluster_type: str,
    description: str = "",
) -> Cluster:
    """创建新集群。"""
    cluster = Cluster(name=name, type=cluster_type, description=description)
    db.add(cluster)
    await db.flush()
    return cluster


async def list_clusters(
    db: AsyncSession,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Cluster], int]:
    """列出集群，支持可选的状态过滤和分页。"""
    stmt = select(Cluster)
    count_stmt = select(func.count(Cluster.id))

    if status:
        stmt = stmt.where(Cluster.status == status)
        count_stmt = count_stmt.where(Cluster.status == status)

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = stmt.order_by(Cluster.id.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    clusters = list(result.scalars())

    return clusters, total


async def get_cluster(db: AsyncSession, cluster_id: int) -> Cluster | None:
    """根据 ID 获取集群。"""
    stmt = select(Cluster).where(Cluster.id == cluster_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_cluster_by_name(db: AsyncSession, name: str) -> Cluster | None:
    """根据名称获取集群。

    默认集群的「身份」是其名称 ``default``，而非写死的自增 id——id 由 DB 分配、
    可变（删后重建会变），故凡需「默认集群」处一律按名解析（init_db seed / 引导流程）。
    """
    stmt = select(Cluster).where(Cluster.name == name)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def update_cluster(
    db: AsyncSession,
    cluster_id: int,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> Cluster | None:
    """更新集群字段（None = 不修改，与 ``UpdateClusterRequest`` 的可选语义一致）。"""
    stmt = select(Cluster).where(Cluster.id == cluster_id)
    result = await db.execute(stmt)
    cluster = result.scalar_one_or_none()
    if cluster:
        if name is not None:
            cluster.name = name
        if description is not None:
            cluster.description = description
        if status is not None:
            cluster.status = status
        await db.flush()
        await db.refresh(cluster)
    return cluster


async def delete_cluster(db: AsyncSession, cluster_id: int) -> bool:
    """删除集群。

    守卫：集群下仍有节点时拒绝（逻辑外键，删了会孤立节点），抛 :class:`Conflict`。
    返回是否实际删除（集群不存在返回 False，由调用方决定 404）。
    """
    cluster = await get_cluster(db, cluster_id)
    if cluster is None:
        return False

    # 零物理 FK：手动校验是否仍有节点引用该集群，避免删集群后留下孤立节点
    node_count_stmt = select(func.count(Node.id)).where(Node.cluster_id == cluster_id)
    node_count = (await db.execute(node_count_stmt)).scalar() or 0
    if node_count > 0:
        raise Conflict(f"集群下仍有 {node_count} 个节点，无法删除")

    await db.delete(cluster)
    await db.flush()
    return True


# ---- 集群关联装配（节点表展示 cluster_name / 集群表展示统计）----


async def get_cluster_name_map(db: AsyncSession, cluster_ids: list[int]) -> dict[int, str]:
    """批量取 ``cluster_id → name`` 映射（供节点表展示所属集群名）。"""
    if not cluster_ids:
        return {}
    stmt = select(Cluster.id, Cluster.name).where(Cluster.id.in_(cluster_ids))
    return {cid: name for cid, name in (await db.execute(stmt)).all()}


async def get_cluster_stats_map(db: AsyncSession, cluster_ids: list[int]) -> dict[int, dict]:
    """批量取 ``cluster_id → {node_count, online_node_count, gpu_count}``。

    节点总数 / 在线数一条 GROUP BY（在线数用 FILTER 子句）；GPU 总数经 node 关联
    后再 GROUP BY。未出现的 cluster_id 由调用方补 0。
    """
    if not cluster_ids:
        return {}
    node_stmt = (
        select(
            Node.cluster_id,
            func.count(Node.id),
            func.count(Node.id).filter(Node.status == NodeStatus.ONLINE),
        )
        .where(Node.cluster_id.in_(cluster_ids))
        .group_by(Node.cluster_id)
    )
    node_map: dict[int, dict] = {
        cid: {"node_count": total, "online_node_count": online}
        for cid, total, online in (await db.execute(node_stmt)).all()
    }
    gpu_stmt = (
        select(Node.cluster_id, func.count(Gpu.id))
        .join(Gpu, Gpu.node_id == Node.id)
        .where(Node.cluster_id.in_(cluster_ids))
        .group_by(Node.cluster_id)
    )
    gpu_map = {cid: cnt for cid, cnt in (await db.execute(gpu_stmt)).all()}
    # 仅返回有节点或 GPU 的集群（稀疏映射，与同模块 get_cluster_name_map 形态一致）；
    # 无节点 / GPU 的集群不在结果中，由 enrich_clusters 的默认值兜底为 0。
    return {
        cid: {
            "node_count": node_map.get(cid, {}).get("node_count", 0),
            "online_node_count": node_map.get(cid, {}).get("online_node_count", 0),
            "gpu_count": gpu_map.get(cid, 0),
        }
        for cid in node_map.keys() | gpu_map.keys()
    }


async def enrich_clusters(db: AsyncSession, clusters: list[Cluster]) -> list[Cluster]:
    """把每个集群的 node_count / online_node_count / gpu_count 批量回填到 ORM 实例上。"""
    if not clusters:
        return clusters
    stats = await get_cluster_stats_map(db, [c.id for c in clusters])
    for c in clusters:
        s = stats.get(c.id, {})
        c.node_count = s.get("node_count", 0)
        c.online_node_count = s.get("online_node_count", 0)
        c.gpu_count = s.get("gpu_count", 0)
    return clusters
