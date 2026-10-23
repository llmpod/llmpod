"""平台全局设置读取端点：GET /api/v1/settings（需登录）。

首屏渲染（登录页品牌/主题/维护横幅）已由 index.html 注入的
``window.__LLMPOD_SETTINGS__`` 覆盖，匿名无需任何接口；本端点仅服务登录后的
主动刷新场景。返回合并后的完整全局设置对象，未配置字段为显式 ``null``
（前端判空统一 ``?? ''``）；维护字段一并下发（会话内展示维护提示用）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import require_authenticated
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.db import get_db
from llmpod.services.settings_service import SettingsPayload, get_settings

router = APIRouter()


@router.get(
    "",
    response_model=Result[SettingsPayload],
    summary="获取平台全局设置",
)
async def get_public_settings(
    db: AsyncSession = Depends(get_db),
    _user: SessionData = Depends(require_authenticated),
):
    """读平台全局设置（任意登录态，不限访问域）。"""
    return success(await get_settings(db))
