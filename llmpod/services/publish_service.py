"""路由管理 service（对外服务 Route + 成员 RouteBinding）。

部署成功钩子调用 ``ensure_route_and_binding``；管理端做改名/定价/上下架/failover/成员权重。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.config.enums import ModelTag, ModelType
from llmpod.core.exceptions import Conflict, LLMPodValidationError, NotFound
from llmpod.models import Route, RouteBinding, TenantModelBlock
from llmpod.models.route import (
    FAILOVER_ENABLED_DEFAULT,
    FAILOVER_MAX_RETRIES_DEFAULT,
)


class _UnsetType:
    """「未传该参数」哨兵：与显式 ``None``（清空）区分，只用作三态更新参数的默认值。

    具名类型（而非裸 ``object()``）是为了 ``int | None | _UnsetType`` 这类标注可读、日志可辨。
    注意 ``api_key_service`` 另有一份同形定义（有意不抽共享模块，两个服务的字段清单
    各自演化）：两个 UNSET 单例**不可跨服务混用**——`is not UNSET` 对别人的哨兵恒真，
    会被当成真实值写进列。
    """

    def __repr__(self) -> str:
        return "UNSET"


#: 三态更新哨兵单例：三态参数的默认值（未传=不改）
UNSET = _UnsetType()


def tri_state(body: BaseModel, field: str) -> Any:
    """请求体三态取值：字段未出现在 body → UNSET（不改）；显式 null → None（清空）；有值 → 原值。

    先取值再判是否出现：字段名写错在 ``getattr`` 处即 AttributeError 响亮失败，而非静默
    退化成 UNSET（「清空无效」bug 的形状）。前提：请求模型不得带会回填字段的 validator，
    否则 model_fields_set 失真。
    """
    value = getattr(body, field)
    return value if field in body.model_fields_set else UNSET


def _validate_tags(tags: list[str] | None) -> None:
    """校验能力标签 ∈ 受控词表（ModelTag）；未知值 raise 领域异常（手填防散落）。"""
    if not tags:
        return
    allowed = ModelTag.values()
    unknown = sorted(set(tags) - set(allowed))
    if unknown:
        raise LLMPodValidationError(f"未知标签: {'、'.join(unknown)}；可用标签: {'、'.join(allowed)}")


async def get_route_by_name(db: AsyncSession, name: str) -> Route | None:
    return (await db.execute(select(Route).where(Route.name == name))).scalar_one_or_none()


async def create_route(
    db: AsyncSession,
    *,
    name: str,
    model_type: str = "llm",
    description: str | None = None,
    context_length: int | None = None,
    tags: list | None = None,
    input_price_per_mtok=None,
    output_price_per_mtok=None,
    cached_input_price_per_mtok=None,
    failover_enabled: bool = FAILOVER_ENABLED_DEFAULT,
    failover_max_retries: int = FAILOVER_MAX_RETRIES_DEFAULT,
    bindings: list | None = None,
) -> Route:
    """管理端手动建路由（部署自动发布之外的入口，如聚合外部端点）。

    与幂等的 ``ensure_route`` 不同：重名直接 Conflict，不吞已有路由。
    ``bindings`` 为可选的初始成员（[{endpoint_id, weight?, priority?}]，缺省
    weight=1 / priority=0），创建即挂上，省得建完再逐个 add。
    """
    _validate_tags(tags)
    existing = await get_route_by_name(db, name)
    if existing is not None:
        raise Conflict(f"路由 {name!r} 已存在")
    route = Route(
        name=name,
        status="published",
        origin="manual",
        model_type=model_type,
        description=description,
        context_length=context_length,
        tags=tags or [],
        input_price_per_mtok=input_price_per_mtok,
        output_price_per_mtok=output_price_per_mtok,
        cached_input_price_per_mtok=cached_input_price_per_mtok,
        failover_enabled=failover_enabled,
        failover_max_retries=failover_max_retries,
    )
    db.add(route)
    await db.flush()
    for b in bindings or []:
        db.add(
            RouteBinding(
                route_id=route.id,
                endpoint_id=b.endpoint_id,
                weight=b.weight if b.weight is not None else 1,
                priority=b.priority if b.priority is not None else 0,
                enabled=True,
            )
        )
    if bindings:
        await db.flush()
    return route


def _to_decimal(value) -> Decimal | None:
    """float（spec JSONB 形态）→ Decimal（Route 列 Numeric）；None 透传。

    ``Decimal(str(value))`` 保 6 位小数精度（spec 存 JSONB，Decimal 无法 json 序列化，
    故发布配置用 float；此处转回权威 Decimal 列）。
    """
    return Decimal(str(value)) if value is not None else None


async def ensure_route(
    db: AsyncSession,
    *,
    name: str,
    context_length: int | None = None,
    model_type: str | None = None,
    description: str | None = None,
    tags: list | None = None,
    input_price_per_mtok=None,
    output_price_per_mtok=None,
    cached_input_price_per_mtok=None,
) -> Route:
    """幂等确保路由存在（部署 READY 自动发布钩子调用）。已存在原样返回，不翻动管理员调整。

    新建时 ``origin="auto"``（随部署删除级联清理）；已存在（含管理员预建的 manual 路由）不改 origin，
    也不覆盖管理员手动设置的 context_length/定价/标签——幂等语义与 ensure_binding 一致。
    """
    _validate_tags(tags)
    route = await get_route_by_name(db, name)
    if route is None:
        route = Route(
            name=name,
            status="published",
            origin="auto",
            context_length=context_length,
            model_type=model_type or ModelType.LLM.value,
            description=description,
            tags=tags or [],
            input_price_per_mtok=_to_decimal(input_price_per_mtok),
            output_price_per_mtok=_to_decimal(output_price_per_mtok),
            cached_input_price_per_mtok=_to_decimal(cached_input_price_per_mtok),
            failover_enabled=FAILOVER_ENABLED_DEFAULT,
            failover_max_retries=FAILOVER_MAX_RETRIES_DEFAULT,
        )
        db.add(route)
        await db.flush()
    return route


async def _find_binding(db: AsyncSession, route_id: int, endpoint_id: int) -> RouteBinding | None:
    """按 route_id 单等值查询后 Python 过滤 endpoint_id（FakeSession 不支持复合 where）。"""
    bindings = list((await db.execute(select(RouteBinding).where(RouteBinding.route_id == route_id))).scalars())
    return next((b for b in bindings if b.endpoint_id == endpoint_id), None)


async def _get_binding(db: AsyncSession, *, route_id: int, endpoint_id: int) -> RouteBinding:
    """取路由成员；不存在一律 NotFound（单一文案，与 _get_route 同形）。"""
    binding = await _find_binding(db, route_id, endpoint_id)
    if binding is None:
        raise NotFound(f"路由成员不存在: route_id={route_id} endpoint_id={endpoint_id}")
    return binding


async def ensure_binding(
    db: AsyncSession, *, route_id: int, endpoint_id: int, weight: int = 1, priority: int = 0
) -> RouteBinding:
    """幂等确保成员存在；已存在原样返回（不翻动管理员调整的权重/优先级）。"""
    binding = await _find_binding(db, route_id, endpoint_id)
    if binding is None:
        binding = RouteBinding(
            route_id=route_id, endpoint_id=endpoint_id, weight=weight, priority=priority, enabled=True
        )
        db.add(binding)
        await db.flush()
    return binding


async def ensure_route_and_binding(db: AsyncSession, *, name: str, endpoint_id: int) -> tuple[Route, RouteBinding]:
    """部署成功钩子：确保路由 + append 成员（默认 weight=1）。"""
    route = await ensure_route(db, name=name)
    binding = await ensure_binding(db, route_id=route.id, endpoint_id=endpoint_id)
    return route, binding


async def list_routes(db: AsyncSession) -> list[Route]:
    return list((await db.execute(select(Route))).scalars())


async def _get_route(db: AsyncSession, route_id: int) -> Route:
    route = await db.get(Route, route_id)
    if route is None:
        raise NotFound(f"路由不存在: id={route_id}")
    return route


async def unpublish_route(db: AsyncSession, *, route_id: int) -> Route:
    route = await _get_route(db, route_id)
    route.status = "unpublished"
    await db.flush()
    return route


async def republish_route(db: AsyncSession, *, route_id: int) -> Route:
    route = await _get_route(db, route_id)
    route.status = "published"
    await db.flush()
    return route


async def rename_route(db: AsyncSession, *, route_id: int, new_name: str) -> Route:
    route = await _get_route(db, route_id)
    if route.name != new_name:
        existing = await get_route_by_name(db, new_name)
        if existing is not None:
            raise Conflict(f"路由 {new_name!r} 已存在")
        route.name = new_name
        await db.flush()
    return route


async def update_route(
    db: AsyncSession,
    *,
    route_id: int,
    name: str | None = None,
    model_type: str | None = None,
    description: str | None = None,
    context_length: int | None | _UnsetType = UNSET,
    tags: list | None = None,
    input_price_per_mtok: Decimal | None | _UnsetType = UNSET,
    output_price_per_mtok: Decimal | None | _UnsetType = UNSET,
    cached_input_price_per_mtok: Decimal | None | _UnsetType = UNSET,
    failover_enabled: bool | None = None,
    failover_max_retries: int | None | _UnsetType = UNSET,
) -> Route:
    """路由调整：改名/类型/描述/上下文长度/能力标签/定价/发布态/failover。

    两类字段走 ``tri_state`` 三态（不传=不改 / 显式 null=清空 / 传值=覆写），区别只在
    「清空」落到哪：``context_length`` / 定价三列可空，落到 NULL（未设置 / 不计费）；
    ``failover_max_retries`` 是非空列，null 无处可落，落到 0（不切换）。其余仍为 ``None``
    = 不修改：``name`` / ``model_type`` / ``tags`` / ``failover_enabled`` 是 NOT NULL 列；
    ``description`` 虽可空，但更新路径恒收到空串且空串本就穿得过去（清空信号是空串，
    非 null），故不经哨兵。
    """
    _validate_tags(tags)
    route = await _get_route(db, route_id)
    if name is not None and name != route.name:
        route = await rename_route(db, route_id=route_id, new_name=name)
    if model_type is not None:
        route.model_type = model_type
    if description is not None:
        route.description = description
    if context_length is not UNSET:
        route.context_length = context_length
    if tags is not None:
        route.tags = tags
    if input_price_per_mtok is not UNSET:
        route.input_price_per_mtok = input_price_per_mtok
    if output_price_per_mtok is not UNSET:
        route.output_price_per_mtok = output_price_per_mtok
    if cached_input_price_per_mtok is not UNSET:
        route.cached_input_price_per_mtok = cached_input_price_per_mtok
    if failover_enabled is not None:
        route.failover_enabled = failover_enabled
    if failover_max_retries is not UNSET:
        # 显式 null（表单清空）= 0 不切换；列 NOT NULL，null 无处可落
        route.failover_max_retries = 0 if failover_max_retries is None else failover_max_retries
    await db.flush()
    return route


async def list_bindings(db: AsyncSession, *, route_id: int) -> list[RouteBinding]:
    return list((await db.execute(select(RouteBinding).where(RouteBinding.route_id == route_id))).scalars())


async def list_bindings_for_routes(db: AsyncSession, route_ids: list[int]) -> dict[int, list[RouteBinding]]:
    """按路由 id 批量取绑定（列表装配用）：一条 in_ 查询，Python 侧按 route_id 分组。

    逐路由调 list_bindings 是 N+1（管理端发布列表每页 N 条路由 = N 次串行往返）。
    """
    if not route_ids:
        return {}
    rows = list((await db.execute(select(RouteBinding).where(RouteBinding.route_id.in_(route_ids)))).scalars())
    grouped: dict[int, list[RouteBinding]] = {}
    for b in rows:
        grouped.setdefault(b.route_id, []).append(b)
    return grouped


async def upsert_binding(
    db: AsyncSession,
    *,
    route_id: int,
    endpoint_id: int,
    weight: int | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
) -> RouteBinding:
    """成员 upsert：不存在则建（weight=1），存在则改 weight/priority/enabled。"""
    binding = await _find_binding(db, route_id, endpoint_id)
    if binding is None:
        binding = RouteBinding(route_id=route_id, endpoint_id=endpoint_id, weight=1, priority=0, enabled=True)
        db.add(binding)
    if weight is not None:
        binding.weight = weight
    if priority is not None:
        binding.priority = priority
    if enabled is not None:
        binding.enabled = enabled
    await db.flush()
    return binding


async def delete_binding(db: AsyncSession, *, route_id: int, endpoint_id: int) -> None:
    """移除成员；绑定不存在 → NotFound 404（与 models/clusters 删除同语义，
    幂等成功会让打错 id 与真实删除无从区分）。"""
    binding = await _get_binding(db, route_id=route_id, endpoint_id=endpoint_id)
    await db.delete(binding)
    await db.flush()


async def update_binding(
    db: AsyncSession,
    *,
    route_id: int,
    endpoint_id: int,
    weight: int | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
) -> RouteBinding:
    """成员调整（weight/priority/启用开关）；None=不改。成员不存在直接 NotFound。"""
    binding = await _get_binding(db, route_id=route_id, endpoint_id=endpoint_id)
    if weight is not None:
        binding.weight = weight
    if priority is not None:
        binding.priority = priority
    if enabled is not None:
        binding.enabled = enabled
    await db.flush()
    return binding


async def delete_route(db: AsyncSession, *, route_id: int) -> None:
    """删除路由（幂等）；先清成员与租户封禁引用（逻辑外键无级联），再删路由行。

    管理员手动建的路由才有删除场景；部署自动发布的路由删除后会被 READY 钩子
    重新 ensure 回来（如需永久下线用 unpublish）。
    """
    route = await _get_route(db, route_id)
    # 逻辑外键无级联：成员与租户封禁引用各一条批量 DML 清掉（逐行 db.delete 是
    # 每行一次往返），再删路由行本体
    await db.execute(delete(RouteBinding).where(RouteBinding.route_id == route_id))
    await db.execute(delete(TenantModelBlock).where(TenantModelBlock.route_id == route_id))
    await db.delete(route)
    await db.flush()
