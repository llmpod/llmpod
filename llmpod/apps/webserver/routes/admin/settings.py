"""全局设置管理端路由：回显读 + 部分更新写。

读写走 :mod:`llmpod.services.settings_service`。写为**三态部分更新**：请求体
键缺省=不动 / null 或空串=清空 / 有值=覆写（非法有值 raise 422，不清空已存值）。
事务边界在路由层（service 只 flush），与全仓「路由层持请求域 commit」约定一致；
审计对三态端点用 ``exclude_unset`` 序列化，显式 null（如关维护）能留痕。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.db import get_db
from llmpod.permission.permissions import SETTINGS_READ, SETTINGS_UPDATE
from llmpod.services.audit_service import audit_action
from llmpod.services.settings_service import (
    SETTINGS_KEY,
    SettingsPayload,
    get_settings_meta,
    update_settings,
)

router = APIRouter()


class SettingsUpdateRequest(SettingsPayload):
    """部分更新请求体：合法键集合单点继承自 ``SettingsPayload``。

    ``model_dump(exclude_unset=True)`` 区分「没传」与「传 null」——缺省键保持库中值、
    显式 null/空串清空、有值覆写。字段级格式校验（hex / mode / URL scheme）由 service
    写路径执行——非法有值 raise 422。
    """


class SettingsAdminData(SettingsPayload):
    """管理端回显：全局设置字段 + 更新元数据。"""

    updated_by: int | None = Field(None, description="最后更新人用户ID（可空）")
    updated_at: datetime | None = Field(None, description="最后更新时间（可空）")


@router.get(
    "",
    response_model=Result[SettingsAdminData],
    summary="获取全局设置（管理端）",
    dependencies=[Depends(require_permission(SETTINGS_READ))],
)
async def get_admin_settings(
    db: AsyncSession = Depends(get_db),
):
    """管理端回显读：service 一次点查取 payload + 更新元数据。

    不走 TTL 缓存——管理端低频读，取库中当前值保证表单回显即最新。
    """
    payload, updated_by, updated_at = await get_settings_meta(db)
    data = payload.model_dump()
    data["updated_by"] = updated_by
    data["updated_at"] = updated_at
    return success(SettingsAdminData(**data))


@router.post(
    "/update",
    response_model=Result[SettingsPayload],
    summary="部分更新全局设置",
    dependencies=[Depends(require_permission(SETTINGS_UPDATE))],
)
@audit_action(
    action="update",
    resource="settings",
    resource_id=lambda *a, **k: SETTINGS_KEY,
    params_exclude_unset=True,  # 显式 null（关维护开关等）在审计 params 留痕
)
async def update_admin_settings(
    request: SettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """三态部分更新（键缺省=不动 / null·空串=清空 / 有值=覆写），返回合并后全量。"""
    changes: dict[str, Any] = request.model_dump(exclude_unset=True)
    updated = await update_settings(db, changes, updated_by=user.user_id)
    await db.commit()  # 事务边界：service 只 flush，路由层 commit（对齐全仓约定）
    return success(updated)
