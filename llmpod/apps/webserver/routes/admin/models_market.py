"""管理员「模型市场」列表：全部路由（含下架），纯目录视角（不含 bindings）。

与发布管理（/admin/publishes，带成员绑定）分工：两者都是含下架的全集，差别只在
是否附带端点成员绑定——这里只给前端「模型市场」页展示模型目录（对外名/类型/
描述/扩展元数据/定价/发布态），不看租户封禁，下架模型靠 status 区分。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import require_permission
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.out_models import RouteOut
from llmpod.db import get_db
from llmpod.permission.permissions import PUBLISH_READ
from llmpod.services.publish_service import list_routes

log = logging.getLogger("llmpod.apps.webserver.admin.models_market")

router = APIRouter()


@router.get("", response_model=Result[list[RouteOut]], summary="列出全部模型（模型市场）")
async def list_market_models(
    _perm: None = Depends(require_permission(PUBLISH_READ)),
    db: AsyncSession = Depends(get_db),
):
    """全部路由（published + unpublished），不按租户封禁过滤。

    管理员视角 = 全集：含下架（unpublished）与已被某租户封禁的模型；前端按
    ``status`` 打「已下架」标记。字段与租户侧 /tenant/models 同源（RouteOut），
    额外多出下架条目。
    """
    routes = await list_routes(db)
    return success([RouteOut.model_validate(r) for r in routes])
