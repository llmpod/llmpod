"""租户可用模型路由（模型中心视角）。

默认放行：列出全部已发布路由 − 本租户被管理员封禁（TenantModelBlock active）的路由，
即租户 Key 实际可调用的模型全集，附类型/描述与定价（租户侧只读，类型与定价由平台在发布时统一定）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import current_tenant_id, require_permission
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.out_models import RouteOut
from llmpod.db import get_db
from llmpod.models import Route
from llmpod.permission.permissions import TENANT_MODEL_READ
from llmpod.services.model_block_service import active_blocked_route_ids

log = logging.getLogger("llmpod.apps.webserver.tenant.models")

router = APIRouter()


@router.get("", response_model=Result[list[RouteOut]], summary="列出本租户可用模型")
async def list_available_models(
    _perm: None = Depends(require_permission(TENANT_MODEL_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
):
    """本租户可调用模型全集 = 已发布（published）路由 − 本租户封禁（active block）。

    默认放行：所有 published 路由默认可见/可用，仅被管理员封禁的路由排除。
    含类型（model_type）/描述/定价（每百万 token 单位），供「模型中心」页展示与
    计费预估；未发布或已封禁的路由不出现。封禁集口径经 model_block_service
    （与租户看板同源）。
    """
    blocked_ids = await active_blocked_route_ids(db, tenant_id)
    # FakeSession 兼容：in_ 过滤在桩上不可靠，统一取行后 Python 过滤（封禁集
    # 很小，无性能问题）；SQL 侧等价于 published 且不在封禁集内。
    routes = list((await db.execute(select(Route).where(Route.status == "published"))).scalars())
    available = [r for r in routes if r.id not in blocked_ids]
    return success([RouteOut.model_validate(r) for r in available])
