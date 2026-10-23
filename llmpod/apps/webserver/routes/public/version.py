"""版本检查更新端点：GET /api/v1/version/latest（需登录）。

前端关于页与布局红点共用（tenant/admin 任意登录态均可访问）。响应里
``latest_version`` 只在确有新版本时高于当前版本，无更新/禁用/上游失败时
恒等于 ``current_version``——前端拿它判等即可，无需自行比较。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from llmpod.apps.webserver.auth.deps import require_authenticated
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.services.version_service import UpdateCheckData, check_update

router = APIRouter()


@router.get(
    "/latest",
    response_model=Result[UpdateCheckData],
    summary="检查更新",
)
async def get_latest_version(
    _user: SessionData = Depends(require_authenticated),
):
    """对比更新源 manifest 与当前运行版本，返回是否有新版本及更新内容。"""
    return success(await check_update())
