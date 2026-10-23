"""GPU 查询路由。

GPU 为节点上报的派生资源（由 ``node_service.sync_gpus`` 经隧道同步），
故本路由仅提供读取（list / get），不提供 create / update / delete——
手改会被节点下次上报覆盖，徒增不一致。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import require_permission
from llmpod.apps.webserver.schemas import Result, success
from llmpod.core.exceptions import NotFound
from llmpod.db import get_db
from llmpod.models.gpu import Gpu
from llmpod.apps.webserver.schemas.response import PaginatedResponse
from llmpod.apps.webserver.schemas.out_models import GpuDetailOut
from llmpod.permission.permissions import GPU_READ
from llmpod.services.node_service import enrich_gpus, enrich_gpus_runtime

log = logging.getLogger("llmpod.apps.webserver.routes.admin.gpu")

router = APIRouter()


@router.get(
    "",
    response_model=PaginatedResponse[GpuDetailOut],
    summary="列出GPU",
    dependencies=[Depends(require_permission(GPU_READ))],
)
async def list_gpus(
    node_id: str | None = Query(None, description="节点ID"),
    status: str | None = Query(None, description="GPU状态"),
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(50, ge=1, le=100, description="每页条数，最大100"),
    db: AsyncSession = Depends(get_db),
):
    """列出所有 GPU，支持按条件过滤。"""
    stmt = select(Gpu)
    count_stmt = select(func.count(Gpu.id))

    if node_id:
        stmt = stmt.where(Gpu.node_id == node_id)
        count_stmt = count_stmt.where(Gpu.node_id == node_id)
    if status:
        stmt = stmt.where(Gpu.status == status)
        count_stmt = count_stmt.where(Gpu.status == status)

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = stmt.order_by(Gpu.id.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    gpus = list(result.scalars())
    # 批量装配 node_hostname / cluster 信息（一条 join，零 N+1），供前端表直接显示归属
    gpus = await enrich_gpus(db, gpus)
    # 装配实时指标摘要（读 Manager 内存缓冲，无数据 runtime=None）
    gpus = enrich_gpus_runtime(gpus)

    return PaginatedResponse.result(items=gpus, total=total, page=page, page_size=page_size)


@router.get(
    "/{gpu_id}",
    response_model=Result[GpuDetailOut],
    summary="获取GPU详情",
    dependencies=[Depends(require_permission(GPU_READ))],
)
async def get_gpu(
    gpu_id: Annotated[int, Path(description="GPU ID（自增主键）")],
    db: AsyncSession = Depends(get_db),
):
    """根据 ID 获取 GPU。"""
    stmt = select(Gpu).where(Gpu.id == gpu_id)
    gpu = (await db.execute(stmt)).scalar_one_or_none()
    if not gpu:
        raise NotFound("Gpu", gpu_id)
    await enrich_gpus(db, [gpu])
    enrich_gpus_runtime([gpu])
    return success(gpu)
