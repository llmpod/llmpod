"""管理员路由管理（Route 对外服务 + RouteBinding 成员权重/failover）。

仅 GET/POST：写操作一律 POST 动作路径（/{id}/update、/{id}/delete、
/{id}/bindings/{endpoint_id}/{update|enable|disable|delete} 等），
不使用 PATCH/PUT/DELETE。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated
from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.request import INT32_MAX, NUMERIC_12_6_MAX
from llmpod.apps.webserver.schemas.out_models import PublishOut, RouteBindingOut, RouteOut
from llmpod.config.enums import ModelType
from llmpod.db import get_db
from llmpod.permission.permissions import PUBLISH_READ, PUBLISH_UPDATE
from llmpod.services.audit_service import audit_action
from llmpod.services.publish_service import (
    create_route,
    delete_binding,
    delete_route,
    list_bindings,
    list_bindings_for_routes,
    list_routes,
    republish_route,
    tri_state,
    unpublish_route,
    update_binding,
    update_route,
    upsert_binding,
)

log = logging.getLogger("llmpod.apps.webserver.admin.publishes")

router = APIRouter()


class CreateBindingSpec(BaseModel):
    """创建路由时随请求挂上的初始成员。"""

    endpoint_id: int = Field(..., description="端点ID（→ model_endpoint.id）")
    weight: int | None = Field(None, ge=1, le=INT32_MAX, description="协议内权重，缺省 1")
    priority: int | None = Field(None, le=INT32_MAX, description="优先级，缺省 0")


class CreateRouteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="对外模型名（client 请求的 model 字段，全局唯一）")
    model_type: str = Field(
        ModelType.LLM.value,
        max_length=50,
        description="模型类别：llm | multimodal | embedding | rerank | image | stt | tts",
    )
    description: str | None = Field(None, description="模型描述（模型中心展示，可空）")
    context_length: int | None = Field(
        None, gt=0, le=INT32_MAX, description="最大上下文长度（tokens；部署自动发布会带入，管理端可改，可空）"
    )
    tags: list[str] = Field(default_factory=list, description="能力标签（展示用，系统不处理；如 思考/工具调用/视觉）")
    input_price_per_mtok: Decimal | None = Field(
        None,
        ge=0,
        le=NUMERIC_12_6_MAX,
        description="输入 token 定价（每百万；chat 输入与 embeddings/rerank 全量均按此计）",
    )
    output_price_per_mtok: Decimal | None = Field(
        None, ge=0, le=NUMERIC_12_6_MAX, description="输出 token 定价（每百万，仅生成类）"
    )
    cached_input_price_per_mtok: Decimal | None = Field(
        None, ge=0, le=NUMERIC_12_6_MAX, description="缓存命中输入定价（每百万）"
    )
    failover_enabled: bool = Field(True, description="失败切换开关（默认开；单成员路由退化行为与关闭一致）")
    failover_max_retries: int = Field(2, ge=0, le=INT32_MAX, description="切换次数上限（默认 2，共 3 次尝试）")
    bindings: list[CreateBindingSpec] = Field(
        default_factory=list, description="初始端点成员列表（可空，创建后可再绑）"
    )


class UpdateRouteRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255, description="对外模型名，不传则不改")
    model_type: str | None = Field(None, max_length=50, description="模型类别，不传则不改")
    description: str | None = Field(None, description="模型描述，不传则不改（空串=清空）")
    context_length: int | None = Field(
        None, gt=0, le=INT32_MAX, description="最大上下文长度（tokens），不传则不改；传 null=清空（未设置）"
    )
    tags: list[str] | None = Field(None, description="能力标签，不传则不改（传空数组=清空）")
    input_price_per_mtok: Decimal | None = Field(
        None,
        ge=0,
        le=NUMERIC_12_6_MAX,
        description="输入 token 定价（每百万；chat 输入与 embeddings/rerank 全量均按此计），"
        "不传则不改；传 null=清空（不计费）",
    )
    output_price_per_mtok: Decimal | None = Field(
        None,
        ge=0,
        le=NUMERIC_12_6_MAX,
        description="输出 token 定价（每百万，仅生成类），不传则不改；传 null=清空（不计费）",
    )
    cached_input_price_per_mtok: Decimal | None = Field(
        None,
        ge=0,
        le=NUMERIC_12_6_MAX,
        description="缓存命中输入定价（每百万），不传则不改；传 null=清空（按全价计）",
    )
    failover_enabled: bool | None = Field(None, description="失败切换开关，不传则不改")
    failover_max_retries: int | None = Field(
        None, ge=0, le=INT32_MAX, description="切换次数上限，不传则不改；传 null=0（不切换）"
    )


class UpsertBindingRequest(BaseModel):
    weight: int | None = Field(None, ge=1, le=INT32_MAX, description="协议内权重，不传则不改")
    priority: int | None = Field(None, le=INT32_MAX, description="优先级，不传则不改")
    enabled: bool | None = Field(None, description="成员开关，不传则不改")


class UpdateBindingRequest(BaseModel):
    weight: int | None = Field(None, ge=1, le=INT32_MAX, description="协议内权重，不传则不改")
    priority: int | None = Field(None, le=INT32_MAX, description="优先级，不传则不改")


async def _publish_out(db: AsyncSession, route) -> PublishOut:
    bindings = await list_bindings(db, route_id=route.id)
    return PublishOut(
        route=RouteOut.model_validate(route),
        bindings=[RouteBindingOut.model_validate(b) for b in bindings],
    )


@router.post("", response_model=Result[PublishOut], summary="创建路由（发布）")
@audit_action(action="create", resource="publish", resource_id=lambda r: r["data"]["route"]["id"])
async def create_publish(
    body: CreateRouteRequest,
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """创建一条对外服务路由：对外模型名（全局唯一）+ 定价 + failover 配置 + 可选初始端点成员。

    新建即 published（可见于 /v1/models）；空价格 = 对应项不计费。``bindings``
    可携带初始端点成员（endpoint_id + 可选 weight/priority），创建即挂上；
    端点不存在不阻断创建（绑定在快照装配时自然无候选）。
    """
    route = await create_route(
        db,
        name=body.name,
        model_type=body.model_type,
        description=body.description,
        context_length=body.context_length,
        tags=body.tags,
        input_price_per_mtok=body.input_price_per_mtok,
        output_price_per_mtok=body.output_price_per_mtok,
        cached_input_price_per_mtok=body.cached_input_price_per_mtok,
        failover_enabled=body.failover_enabled,
        failover_max_retries=body.failover_max_retries,
        bindings=body.bindings,
    )
    await db.commit()
    # flush 不带回 onupdate 生成的 updated_at（INSERT 则价格列未加载），序列化前 refresh 补齐，避免 MissingGreenlet → 500
    await db.refresh(route)
    return success(await _publish_out(db, route))


@router.get("", response_model=Result[list[PublishOut]], summary="列出全部发布")
async def list_publishes(
    _perm: None = Depends(require_permission(PUBLISH_READ)),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(None, description="按状态过滤：published | unpublished | all；缺省=全集"),
):
    """列出路由及其端点绑定，供发布管理页展示。

    缺省返回全集（含已下架）——发布管理页要对已下架路由再上架，下架后条目
    必须仍在列表里；``?status=published`` 只看在线路由，``?status=unpublished``
    只看下架条目。
    """
    routes = await list_routes(db)
    if status and status != "all":
        routes = [r for r in routes if r.status == status]
    bindings_by_route = await list_bindings_for_routes(db, [r.id for r in routes])
    items = [
        PublishOut(
            route=RouteOut.model_validate(r),
            bindings=[RouteBindingOut.model_validate(b) for b in bindings_by_route.get(r.id, [])],
        )
        for r in routes
    ]
    return success(items)


@router.post("/{route_id}/unpublish", response_model=Result[RouteOut], summary="下架路由")
@audit_action(action="update", resource="publish", resource_id="route_id")
async def unpublish(
    route_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """下架：status → unpublished。网关 /v1/models 不再列出、调用返回 404；
    绑定与定价保留，重新发布即恢复。"""
    route = await unpublish_route(db, route_id=route_id)
    await db.commit()
    # flush 不带回 onupdate 生成的 updated_at，序列化前 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(route)
    return success(RouteOut.model_validate(route))


@router.post("/{route_id}/republish", response_model=Result[RouteOut], summary="重新发布路由")
@audit_action(action="update", resource="publish", resource_id="route_id")
async def republish(
    route_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """重新上架：status → published（绑定与定价沿用下架前的配置）。"""
    route = await republish_route(db, route_id=route_id)
    await db.commit()
    # flush 不带回 onupdate 生成的 updated_at，序列化前 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(route)
    return success(RouteOut.model_validate(route))


@router.post("/{route_id}/update", response_model=Result[RouteOut], summary="更新路由定价与 failover")
@audit_action(
    action="update",
    resource="publish",
    resource_id="route_id",
    params_exclude_unset=True,  # 三态更新：显式 null=清空须与「未传」在审计里可区分
)
async def update_publish(
    route_id: Annotated[int, Path()],
    body: UpdateRouteRequest,
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新对外名 / 定价三列（输入 / 输出 / 缓存命中，每百万 token 单位）/ failover。

    input 价同时覆盖 embeddings/rerank 的计费（Route = 单模型，输入 token 单价
    唯一）；价格为空 = 不计费；定价变更经快照轮询热更新（默认数秒内生效）。
    可空字段（上下文长度 / 定价三列）三态：不传=不改，传 null=清空（未设置 / 不计费）；
    ``failover_max_retries`` 同为三态，但它非空列，传 null=0（不切换）。
    """
    route = await update_route(
        db,
        route_id=route_id,
        name=body.name,
        model_type=body.model_type,
        description=body.description,
        context_length=tri_state(body, "context_length"),
        tags=body.tags,
        input_price_per_mtok=tri_state(body, "input_price_per_mtok"),
        output_price_per_mtok=tri_state(body, "output_price_per_mtok"),
        cached_input_price_per_mtok=tri_state(body, "cached_input_price_per_mtok"),
        failover_enabled=body.failover_enabled,
        failover_max_retries=tri_state(body, "failover_max_retries"),
    )
    await db.commit()
    # flush 不带回 onupdate 生成的 updated_at，序列化前 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(route)
    return success(RouteOut.model_validate(route))


@router.post(
    "/{route_id}/bindings/{endpoint_id}", response_model=Result[RouteBindingOut], summary="绑定端点到路由（upsert）"
)
@audit_action(action="update", resource="publish", resource_id="route_id")
async def upsert_binding_endpoint(
    route_id: Annotated[int, Path()],
    endpoint_id: Annotated[int, Path()],
    body: UpsertBindingRequest,
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """绑定端点为路由候选来源（已存在则更新参数）：``weight`` 协议内权重、
    ``priority`` 高的组优先承接；同一路由可绑多个端点组成故障转移池。"""
    binding = await upsert_binding(
        db,
        route_id=route_id,
        endpoint_id=endpoint_id,
        weight=body.weight,
        priority=body.priority,
        enabled=body.enabled,
    )
    await db.commit()
    # flush 不带回 onupdate 生成的 updated_at，序列化前 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(binding)
    return success(RouteBindingOut.model_validate(binding))


@router.post(
    "/{route_id}/bindings/{endpoint_id}/update", response_model=Result[RouteBindingOut], summary="调整绑定权重/优先级"
)
@audit_action(action="update", resource="publish", resource_id="route_id")
async def update_binding_endpoint(
    route_id: Annotated[int, Path()],
    endpoint_id: Annotated[int, Path()],
    body: UpdateBindingRequest,
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """成员调整（weight/priority）；成员不存在 404（不存在即建请走 upsert 端点）。"""
    binding = await update_binding(
        db,
        route_id=route_id,
        endpoint_id=endpoint_id,
        weight=body.weight,
        priority=body.priority,
    )
    await db.commit()
    # flush 不带回 onupdate 生成的 updated_at，序列化前 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(binding)
    return success(RouteBindingOut.model_validate(binding))


@router.post(
    "/{route_id}/bindings/{endpoint_id}/enable", response_model=Result[RouteBindingOut], summary="启用绑定成员"
)
@audit_action(action="update", resource="publish", resource_id="route_id")
async def enable_binding_endpoint(
    route_id: Annotated[int, Path()],
    endpoint_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """成员启用（重新进入快照候选）。"""
    binding = await update_binding(db, route_id=route_id, endpoint_id=endpoint_id, enabled=True)
    await db.commit()
    # flush 不带回 onupdate 生成的 updated_at，序列化前 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(binding)
    return success(RouteBindingOut.model_validate(binding))


@router.post(
    "/{route_id}/bindings/{endpoint_id}/disable", response_model=Result[RouteBindingOut], summary="禁用绑定成员"
)
@audit_action(action="update", resource="publish", resource_id="route_id")
async def disable_binding_endpoint(
    route_id: Annotated[int, Path()],
    endpoint_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """成员禁用（移出快照候选，保留成员记录；区别于 delete 的彻底移除）。"""
    binding = await update_binding(db, route_id=route_id, endpoint_id=endpoint_id, enabled=False)
    await db.commit()
    # flush 不带回 onupdate 生成的 updated_at，序列化前 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(binding)
    return success(RouteBindingOut.model_validate(binding))


@router.post("/{route_id}/bindings/{endpoint_id}/delete", response_model=Result[None], summary="解绑端点")
@audit_action(action="delete", resource="publish", resource_id="route_id")
async def remove_binding(
    route_id: Annotated[int, Path()],
    endpoint_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """彻底移除绑定成员；路由无任何可用端点时调用返回 503。"""
    await delete_binding(db, route_id=route_id, endpoint_id=endpoint_id)
    await db.commit()
    return success(None)


@router.post("/{route_id}/delete", response_model=Result[None], summary="删除路由")
@audit_action(action="delete", resource="publish", resource_id="route_id")
async def remove_publish(
    route_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(PUBLISH_UPDATE)),
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """删除路由：连带清成员与租户授权引用。部署自动发布的路由会被 READY 钩子重建，永久下线用 unpublish。"""
    await delete_route(db, route_id=route_id)
    await db.commit()
    return success(None)
