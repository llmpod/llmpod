"""API 网关授权（AuthZ）：请求管道第 2 级。

检查顺序（快失败）：allowed_models（空=全部模型可用）→ 发布态 →
（租户 Key）TenantModelBlock active 封禁 + 租户未冻结。后两项 v1 走 PG 点查
（变更频率低、查询量小）；网关运行期只点查不做集合包含校验（写入侧已由管理端强制）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.proxyserver.authn import ApiKeyContext
from llmpod.apps.proxyserver.snapshot import RouteEntry
from llmpod.models import Tenant
from llmpod.services.model_block_service import active_blocked_route_ids


class AuthorizationError(Exception):
    """授权失败（路由层转 403，body 注明原因）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def authorize(runtime, db: AsyncSession, ctx: ApiKeyContext, entry: RouteEntry) -> None:
    """授权校验；不通过 raise AuthorizationError。

    封禁集与租户状态优先取配置快照（随 ``runtime`` 的周期轮询热更新，默认 3s）：
    把每请求两次 PG 点查（封禁集 + Tenant.status）挪进快照，管理端变更在至多
    一个轮询周期内生效。快照未覆盖的租户（新建租户/刚产生封禁）回源点查兜底。
    """
    if ctx.allowed_models and entry.name not in ctx.allowed_models:
        raise AuthorizationError(f"model {entry.name!r} not allowed for this api key")

    if entry.status != "published":
        raise AuthorizationError(f"model {entry.name!r} is not published")

    if ctx.tenant_id is None:
        return  # 平台 Key 跳过租户封禁校验

    # 默认放行：不在本租户 active 封禁集内即放行。封禁集与租户状态随配置快照下发
    # （poll_interval_seconds 轮询，默认 3s）——管理端封禁/停用租户后至多一个轮询
    # 周期内仍可能放行新请求；被停用的租户经由下一次快照重建拦截
    snapshot = runtime.snapshot
    blocked = snapshot.tenant_blocked_routes.get(ctx.tenant_id)
    if blocked is None:
        # 快照未覆盖该租户（新租户/刚产生封禁）：回源点查兜底
        blocked = await active_blocked_route_ids(db, ctx.tenant_id)
    if entry.route_id in blocked:
        raise AuthorizationError(f"model {entry.name!r} blocked for tenant")

    tenant_status = snapshot.tenant_statuses.get(ctx.tenant_id)
    if tenant_status is None:
        row = await db.get(Tenant, ctx.tenant_id)
        tenant_status = row.status if row is not None else "missing"
    if tenant_status != "active":
        raise AuthorizationError("tenant disabled")
