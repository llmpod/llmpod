"""集群管理路由。"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.core.exceptions import LLMPodValidationError, NotFound
from llmpod.db import get_db
from llmpod.apps.webserver.schemas.response import PaginatedResponse
from llmpod.apps.webserver.schemas.out_models import ClusterDetailOut
from llmpod.apps.webserver.schemas.topology import ClusterTopology
from llmpod.permission.permissions import (
    CLUSTER_CREATE,
    CLUSTER_DELETE,
    CLUSTER_READ,
    CLUSTER_UPDATE,
)
from llmpod.services.audit_service import audit_action
from llmpod.services.cluster_service import (
    create_cluster,
    delete_cluster,
    enrich_clusters,
    get_cluster,
    list_clusters,
    update_cluster,
)

log = logging.getLogger("llmpod.apps.webserver.routes.admin.cluster")

router = APIRouter()

# 可创建的集群类型：K8s 编排未实现，暂注释掉不开放（DB CHECK 仍保留 'k8s' 枚举占位）；接入后取消注释即恢复
CREATABLE_CLUSTER_TYPES = (
    "docker",
    # "k8s",
)


class CreateClusterRequest(BaseModel):
    name: str = Field(..., description="集群名称")
    type: str = Field(..., description=f"集群类型：{' | '.join(CREATABLE_CLUSTER_TYPES)}")
    description: str = Field("", description="集群描述，可空")


class UpdateClusterRequest(BaseModel):
    name: str | None = Field(None, description="集群名称；不传则不修改")
    description: str | None = Field(None, description="集群描述；不传则不修改")
    status: str | None = Field(None, description="集群状态：active | inactive；不传则不修改")


class ClusterDeleteData(BaseModel):
    """删除集群回执（集群下仍有节点时拒绝）。"""

    id: int = Field(..., description="被删集群ID")
    deleted: bool = Field(True, description="删除是否生效")


@router.get(
    "",
    response_model=PaginatedResponse[ClusterDetailOut],
    summary="列出集群",
    dependencies=[Depends(require_permission(CLUSTER_READ))],
)
async def list_clusters_endpoint(
    status: str | None = Query(None, description="集群状态"),
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，最大100"),
    db: AsyncSession = Depends(get_db),
):
    """列出所有集群，可按状态过滤。"""
    clusters, total = await list_clusters(db, status=status, page=page, page_size=page_size)
    # 批量装配 node_count / online_node_count / gpu_count（各一条 GROUP BY，零 N+1）
    clusters = await enrich_clusters(db, clusters)
    return PaginatedResponse.result(items=clusters, total=total, page=page, page_size=page_size)


# ---- 拓扑图（字面量路由须声明在 /{cluster_id} 之前，否则 /topology 会被参数路由吞掉）----


@router.get(
    "/topology",
    response_model=Result[list[ClusterTopology]],
    summary="全集群拓扑（节点图可视化）",
    dependencies=[Depends(require_permission(CLUSTER_READ))],
)
async def get_all_topologies(db: AsyncSession = Depends(get_db)):
    """全部 active 集群的拓扑数据（前端 buildGraph 组装节点图）。"""
    from llmpod.apps.manager.deployment.topology import build_all_topologies

    topologies = await build_all_topologies(db)
    return success(topologies)


@router.get(
    "/{cluster_id}/topology",
    response_model=Result[ClusterTopology],
    summary="单集群拓扑（节点图可视化）",
    dependencies=[Depends(require_permission(CLUSTER_READ))],
)
async def get_cluster_topology(
    cluster_id: Annotated[int, Path(description="集群ID")],
    db: AsyncSession = Depends(get_db),
):
    """单集群拓扑数据（前端 buildGraph 组装节点图）。"""
    from llmpod.apps.manager.deployment.topology import build_cluster_topology

    topology = await build_cluster_topology(db, cluster_id)
    return success(topology)


@router.get(
    "/{cluster_id}",
    response_model=Result[ClusterDetailOut],
    summary="获取集群详情",
    dependencies=[Depends(require_permission(CLUSTER_READ))],
)
async def get_cluster_endpoint(
    cluster_id: Annotated[int, Path(description="集群ID")],
    db: AsyncSession = Depends(get_db),
):
    """根据 ID 获取集群。"""
    cluster = await get_cluster(db, cluster_id)
    if not cluster:
        raise NotFound("Cluster", cluster_id)
    await enrich_clusters(db, [cluster])
    return success(cluster)


@router.post(
    "",
    response_model=Result[ClusterDetailOut],
    summary="创建集群",
    dependencies=[Depends(require_permission(CLUSTER_CREATE))],
)
@audit_action(action="create", resource="cluster")
async def create_cluster_endpoint(
    request: CreateClusterRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """创建新集群。"""
    if request.type not in CREATABLE_CLUSTER_TYPES:
        raise LLMPodValidationError(
            f"暂不支持的集群类型：{request.type}（当前仅支持 {' | '.join(CREATABLE_CLUSTER_TYPES)}）"
        )
    cluster = await create_cluster(
        db,
        name=request.name,
        cluster_type=request.type,
        description=request.description,
    )
    await db.commit()
    # 新建集群下无节点 / GPU，统计必为 0，直接置默认，避免两条空 GROUP BY 查询
    cluster.node_count = 0
    cluster.online_node_count = 0
    cluster.gpu_count = 0
    return success(cluster)


@router.post(
    "/{cluster_id}/update",
    response_model=Result[ClusterDetailOut],
    summary="更新集群",
    dependencies=[Depends(require_permission(CLUSTER_UPDATE))],
)
@audit_action(action="update", resource="cluster", resource_id="cluster_id")
async def update_cluster_endpoint(
    cluster_id: Annotated[int, Path(description="集群ID")],
    request: UpdateClusterRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新集群（返回更新后的集群）。"""
    cluster = await update_cluster(
        db,
        cluster_id,
        name=request.name,
        description=request.description,
        status=request.status,
    )
    if not cluster:
        raise NotFound("Cluster", cluster_id)
    await db.commit()
    await enrich_clusters(db, [cluster])
    return success(cluster)


@router.post(
    "/{cluster_id}/delete",
    response_model=Result[ClusterDeleteData],
    summary="删除集群",
    dependencies=[Depends(require_permission(CLUSTER_DELETE))],
)
@audit_action(action="delete", resource="cluster", resource_id="cluster_id")
async def delete_cluster_endpoint(
    cluster_id: Annotated[int, Path(description="集群ID")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """删除集群。

    集群下仍有节点时拒绝（零物理 FK，删了会孤立节点）；详见 ``delete_cluster`` 服务。
    """
    deleted = await delete_cluster(db, cluster_id)
    if not deleted:
        raise NotFound("Cluster", cluster_id)
    await db.commit()
    return success(ClusterDeleteData(id=cluster_id))
