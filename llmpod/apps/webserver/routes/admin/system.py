"""系统配置路由。"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.core.exceptions import NotFound
from llmpod.db import get_db
from llmpod.models.system_config import SystemConfig
from llmpod.apps.webserver.schemas.out_models import SystemConfigOut
from llmpod.permission.permissions import SYSTEM_READ, SYSTEM_UPDATE
from llmpod.services.audit_service import audit_action

log = logging.getLogger("llmpod.apps.webserver.routes.admin.system")

router = APIRouter()


class UpdateSystemConfigRequest(BaseModel):
    value: dict = Field(..., description="配置值（JSON 对象）")
    description: str | None = Field(None, description="配置说明，可空")


class ConfigUpdateData(BaseModel):
    """系统配置更新回执（更新或创建）。"""

    key: str = Field(..., description="配置键名")
    updated: bool = Field(True, description="更新是否生效")


@router.get(
    "/config",
    response_model=Result[list[SystemConfigOut]],
    summary="列出系统配置",
    dependencies=[Depends(require_permission(SYSTEM_READ))],
)
async def list_system_config(
    db: AsyncSession = Depends(get_db),
):
    """列出所有系统配置（需要平台权限）。"""
    stmt = select(SystemConfig).order_by(SystemConfig.key)
    result = await db.execute(stmt)
    configs = list(result.scalars())

    return success(configs)


@router.get(
    "/config/{key}",
    response_model=Result[SystemConfigOut],
    summary="获取系统配置",
    dependencies=[Depends(require_permission(SYSTEM_READ))],
)
async def get_system_config(
    key: Annotated[str, Path(description="配置键名")],
    db: AsyncSession = Depends(get_db),
):
    """根据 key 获取系统配置。"""
    stmt = select(SystemConfig).where(SystemConfig.key == key)
    result = await db.execute(stmt)
    config = result.scalar_one_or_none()
    if not config:
        raise NotFound("Config", key)

    return success(config)


@router.post(
    "/config/{key}/update",
    response_model=Result[ConfigUpdateData],
    summary="更新系统配置",
    dependencies=[Depends(require_permission(SYSTEM_UPDATE))],
)
@audit_action(action="update", resource="system", resource_id="key")
async def update_system_config(
    key: Annotated[str, Path(description="配置键名")],
    request: UpdateSystemConfigRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新或创建系统配置。"""
    stmt = select(SystemConfig).where(SystemConfig.key == key)
    result = await db.execute(stmt)
    config = result.scalar_one_or_none()

    if config:
        config.value = request.value
        if request.description is not None:
            config.description = request.description
        config.updated_by = user.user_id
    else:
        config = SystemConfig(
            key=key,
            value=request.value,
            description=request.description or "",
            updated_by=user.user_id,
        )
        db.add(config)

    await db.flush()
    await db.commit()
    return success(ConfigUpdateData(key=key))
