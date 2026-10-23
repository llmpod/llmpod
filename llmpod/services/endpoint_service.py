"""来源端点管理 service（外部 API 登记 / 维护）。

外部端点 = ModelEndpoint(deployment_id=NULL, engine_type="external")，经路由成员挂到
对外服务。删除时先解绑 RouteBinding 成员（逻辑外键无级联）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.core.exceptions import NotFound
from llmpod.models import ModelEndpoint, RouteBinding


async def create_endpoint(
    db: AsyncSession,
    *,
    protocol: str,
    api_base: str,
    served_model: str,
    name: str,
    api_key: str | None = None,
    provider_name: str | None = None,
    path: str | None = None,
    operations: list | None = None,
) -> ModelEndpoint:
    """登记外部端点（deployment_id=NULL，来源=external）。"""
    endpoint = ModelEndpoint(
        deployment_id=None,
        engine_type="external",
        protocol=protocol,
        api_base=api_base,
        path=path,
        api_key=api_key,
        served_model=served_model,
        name=name,
        provider_name=provider_name,
        operations=operations or [],
        capabilities=[],
        enabled=True,
        health_status="healthy",
    )
    db.add(endpoint)
    await db.flush()
    return endpoint


async def list_endpoints(db: AsyncSession) -> list[ModelEndpoint]:
    return list((await db.execute(select(ModelEndpoint).order_by(ModelEndpoint.id))).scalars())


async def get_endpoint(db: AsyncSession, endpoint_id: int) -> ModelEndpoint:
    endpoint = await db.get(ModelEndpoint, endpoint_id)
    if endpoint is None:
        raise NotFound(f"端点不存在: id={endpoint_id}")
    return endpoint


async def update_endpoint(
    db: AsyncSession,
    *,
    endpoint_id: int,
    api_base: str | None = None,
    api_key: str | None = None,
    served_model: str | None = None,
    name: str | None = None,
    provider_name: str | None = None,
    path: str | None = None,
    enabled: bool | None = None,
    health_status: str | None = None,
) -> ModelEndpoint:
    """维护端点；None=不改。"""
    endpoint = await get_endpoint(db, endpoint_id)
    if api_base is not None:
        endpoint.api_base = api_base
    if api_key is not None:
        endpoint.api_key = api_key
    if served_model is not None:
        endpoint.served_model = served_model
    if name is not None:
        endpoint.name = name
    if provider_name is not None:
        endpoint.provider_name = provider_name
    if path is not None:
        endpoint.path = path
    if enabled is not None:
        endpoint.enabled = enabled
    if health_status is not None:
        endpoint.health_status = health_status
    await db.flush()
    return endpoint


async def delete_endpoint(db: AsyncSession, *, endpoint_id: int) -> None:
    """删除端点；仍被 RouteBinding 引用时先解绑成员（悬空绑定会让路由快照残留死条目）。

    端点不存在 → NotFound 404（与 models/clusters 删除同语义；幂等成功会让
    「打错 id 也返回 200」无从与真实删除区分）。
    """
    endpoint = await get_endpoint(db, endpoint_id)
    bindings = list((await db.execute(select(RouteBinding).where(RouteBinding.endpoint_id == endpoint_id))).scalars())
    for b in bindings:
        await db.delete(b)
    await db.delete(endpoint)
    await db.flush()
