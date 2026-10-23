"""租户模型封禁 service（TenantModelBlock 管理端写链路 + 批量读装配）。

默认放行 + 例外封禁：租户默认可用全部已发布路由，本表只记「禁止」集合。
写走 PUT 语义（全量替换 desired set）；读走批量映射（供租户详情/列表装配）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.schemas.out_models import BlockedModelOut
from llmpod.core.exceptions import NotFound
from llmpod.models import Route, Tenant, TenantModelBlock


async def active_blocked_route_ids(db: AsyncSession, tenant_id: int) -> set[int]:
    """该租户当前生效的封禁路由 ID 集（status=active；已解封/Inactive 不计）。

    「published − 生效封禁 = 租户可用模型」的口径唯一定义处——租户模型路由、
    租户看板等消费方都经此取封禁集，不各自复写 block 查询语义。
    """
    rows = (
        await db.execute(
            select(TenantModelBlock.route_id).where(
                TenantModelBlock.tenant_id == tenant_id,
                TenantModelBlock.status == "active",
            )
        )
    ).scalars()
    return set(rows)


async def set_tenant_model_blocks(db: AsyncSession, *, tenant_id: int, route_ids: list[int]) -> list[TenantModelBlock]:
    """全量替换租户封禁路由集合，返回替换后的封禁行。"""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound(f"租户不存在: id={tenant_id}")

    desired = list(dict.fromkeys(route_ids))  # 去重保序
    existing: list[TenantModelBlock] = list(
        (await db.execute(select(TenantModelBlock).where(TenantModelBlock.tenant_id == tenant_id))).scalars()
    )
    by_route = {b.route_id: b for b in existing}

    for b in existing:
        if b.route_id not in desired:
            await db.delete(b)

    kept: list[TenantModelBlock] = []
    for rid in desired:
        b = by_route.get(rid)
        if b is None:
            b = TenantModelBlock(tenant_id=tenant_id, route_id=rid, status="active")
            db.add(b)
        else:
            b.status = "active"
        kept.append(b)
    await db.flush()
    return kept


async def get_blocked_models_map(db: AsyncSession, tenant_ids: list[int]) -> dict[int, list[BlockedModelOut]]:
    """批量取 ``tenant_id → 已解析的封禁模型列表``（route_id + 名称 + 类别）。

    两条 SQL：先按 tenant_id.in_ 拉封禁行、Python 过滤 active；再按 route_id.in_
    拉路由解析出名称/类别。未封禁的租户不出现在结果中（调用方按缺省空列表兜底）。
    """
    if not tenant_ids:
        return {}
    rows = list(
        (await db.execute(select(TenantModelBlock).where(TenantModelBlock.tenant_id.in_(tenant_ids)))).scalars()
    )
    active = [b for b in rows if b.status == "active"]
    if not active:
        return {}
    route_ids = {b.route_id for b in active}
    route_info = {
        r.id: (r.name, r.model_type) for r in (await db.execute(select(Route).where(Route.id.in_(route_ids)))).scalars()
    }
    m: dict[int, list[BlockedModelOut]] = {}
    for b in active:
        name, model_type = route_info.get(b.route_id, ("", ""))
        m.setdefault(b.tenant_id, []).append(BlockedModelOut(route_id=b.route_id, name=name, model_type=model_type))
    return m
