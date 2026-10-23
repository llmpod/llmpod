"""租户 API Key 自助管理路由（本租户作用域）。

与 admin/api_keys 的差异：Key 强制归属当前租户（不能签发平台直发 Key）、
只能看/管自己租户的 Key；签发/调整走同一套 service（封禁排除的校验在 service 内收口）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import current_tenant_id, get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.request import INT32_MAX, INT64_MAX, NUMERIC_20_6_MAX
from llmpod.apps.webserver.schemas.out_models import ApiKeyCreatedOut, ApiKeyOut
from llmpod.core.exceptions import NotFound
from llmpod.db import get_db, get_redis
from llmpod.models import ApiKey
from llmpod.permission.permissions import (
    TENANT_API_KEY_CREATE,
    TENANT_API_KEY_READ,
    TENANT_API_KEY_UPDATE,
)
from llmpod.services.audit_service import audit_action
from llmpod.services.api_key_service import (
    create_api_key,
    delete_api_key,
    disable_api_key,
    enable_api_key,
    list_api_keys,
    reset_budget,
    tri_state,
    update_api_key,
)

log = logging.getLogger("llmpod.apps.webserver.tenant.api_keys")

router = APIRouter()


class CreateTenantKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Key 显示名")
    allowed_models: list[str] = Field(
        default_factory=list,
        description="允许调用的模型别名（缺省/空=本租户全部可用模型；填了则需 ⊆ 本租户可用模型，平台封禁的模型不可填）",
    )
    rpm_limit: int | None = Field(None, gt=0, le=INT32_MAX, description="每分钟请求数上限（空=不限）")
    tpm_limit: int | None = Field(None, gt=0, le=INT32_MAX, description="每分钟 token 上限（空=不限）")
    total_token_quota: int | None = Field(None, gt=0, le=INT64_MAX, description="Key 总 token 配额（空=不限）")
    model_token_quotas: dict[str, int] | None = Field(None, description="按模型的 token 配额映射")
    budget_limit: Decimal | None = Field(None, ge=0, le=NUMERIC_20_6_MAX, description="Key 预算上限（元，空=不限）")
    expires_at: datetime | None = Field(None, description="过期时间（空=永不过期）")


class UpdateTenantKeyRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128, description="不传则不修改")
    allowed_models: list[str] | None = Field(
        None, description="不传则不修改（重新走可用模型校验；传空列表=清空为本租户全部可用模型）"
    )
    rpm_limit: int | None = Field(None, gt=0, le=INT32_MAX, description="不传则不修改；传 null=清空（不限）")
    tpm_limit: int | None = Field(None, gt=0, le=INT32_MAX, description="不传则不修改；传 null=清空（不限）")
    total_token_quota: int | None = Field(None, gt=0, le=INT64_MAX, description="不传则不修改；传 null=清空（不限）")
    model_token_quotas: dict[str, int] | None = Field(None, description="不传则不修改")
    budget_limit: Decimal | None = Field(
        None, ge=0, le=NUMERIC_20_6_MAX, description="不传则不修改；传 null=清空（不限）"
    )
    expires_at: datetime | None = Field(None, description="不传则不修改；传 null=清空（永不过期）")


async def _load_tenant_key(db: AsyncSession, key_id: int, tenant_id: int) -> ApiKey:
    """按 (id, tenant_id) 加载 Key，缺失或跨租户一律 404（作用域强制）。"""
    key = (
        await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if key is None:
        raise NotFound(f"ApiKey 不存在: id={key_id}")
    return key


@router.get("", response_model=Result[list[ApiKeyOut]], summary="列出本租户 API Key")
async def list_tenant_keys(
    _perm: None = Depends(require_permission(TENANT_API_KEY_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int = Depends(current_tenant_id),
    name: str | None = Query(None, description="按显示名模糊过滤（contains）"),
    status: str | None = Query(None, description="按状态过滤：active | disabled"),
):
    """列出当前租户的 Key（含已禁用，按 id 倒序）；返回元数据 + 完整明文。

    作用域硬绑当前租户：不提供 tenant_id 搜索，筛选仅在本租户内生效。
    """
    keys = await list_api_keys(db, tenant_id=tenant_id, name=name, status=status)
    return success([ApiKeyOut.model_validate(k) for k in keys])


@router.post("", response_model=Result[ApiKeyCreatedOut], summary="签发本租户 API Key")
@audit_action(action="create", resource="api_key", resource_id=lambda r: r["data"]["api_key"]["id"])
async def create_tenant_key(
    body: CreateTenantKeyRequest,
    _perm: None = Depends(require_permission(TENANT_API_KEY_CREATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    tenant_id: int = Depends(current_tenant_id),
    user: SessionData = Depends(get_current_user),
):
    """为自己租户签发 Key（归属强制 = 当前租户）。``plaintext`` 回显完整明文
    （与列表同口径）；``allowed_models`` 自动按本租户可用模型校验
    （平台封禁 / 未发布的模型超范围拒绝）。"""
    key, plaintext = await create_api_key(
        db,
        redis,
        tenant_id=tenant_id,
        name=body.name,
        allowed_models=body.allowed_models,
        rpm_limit=body.rpm_limit,
        tpm_limit=body.tpm_limit,
        total_token_quota=body.total_token_quota,
        model_token_quotas=body.model_token_quotas,
        budget_limit=body.budget_limit,
        expires_at=body.expires_at,
    )
    await db.commit()
    await db.refresh(key)
    return success(ApiKeyCreatedOut(api_key=ApiKeyOut.model_validate(key), plaintext=plaintext))


@router.post("/{key_id}/update", response_model=Result[ApiKeyOut], summary="更新本租户 API Key")
@audit_action(
    action="update",
    resource="api_key",
    resource_id="key_id",
    params_exclude_unset=True,  # 三态更新：显式 null=清空须与「未传」在审计里可区分
)
async def update_tenant_key(
    key_id: Annotated[int, Path()],
    body: UpdateTenantKeyRequest,
    _perm: None = Depends(require_permission(TENANT_API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    tenant_id: int = Depends(current_tenant_id),
    user: SessionData = Depends(get_current_user),
):
    """更新自己租户 Key 的元数据（限流/配额/可用模型/预算/过期）；变更后即时生效。

    可空字段（限流/配额/预算/过期时间）三态：不传=不改，传 null=清空（不限/永不过期）。
    """
    await _load_tenant_key(db, key_id, tenant_id)
    key = await update_api_key(
        db,
        redis,
        key_id=key_id,
        name=body.name,
        allowed_models=body.allowed_models,
        model_token_quotas=body.model_token_quotas,
        rpm_limit=tri_state(body, "rpm_limit"),
        tpm_limit=tri_state(body, "tpm_limit"),
        total_token_quota=tri_state(body, "total_token_quota"),
        budget_limit=tri_state(body, "budget_limit"),
        expires_at=tri_state(body, "expires_at"),
    )
    await db.commit()
    return success(ApiKeyOut.model_validate(key))


@router.post("/{key_id}/disable", response_model=Result[ApiKeyOut], summary="禁用本租户 API Key")
@audit_action(action="update", resource="api_key", resource_id="key_id")
async def disable_tenant_key(
    key_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(TENANT_API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    tenant_id: int = Depends(current_tenant_id),
    user: SessionData = Depends(get_current_user),
):
    """禁用后立即失效（DEL 缓存，网关下次请求即 401）；幂等。"""
    await _load_tenant_key(db, key_id, tenant_id)
    key = await disable_api_key(db, redis, key_id=key_id)
    await db.commit()
    return success(ApiKeyOut.model_validate(key))


@router.post("/{key_id}/enable", response_model=Result[ApiKeyOut], summary="启用本租户 API Key")
@audit_action(action="update", resource="api_key", resource_id="key_id")
async def enable_tenant_key(
    key_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(TENANT_API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    tenant_id: int = Depends(current_tenant_id),
    user: SessionData = Depends(get_current_user),
):
    """重新启用被禁用的 Key（置 active + DEL 缓存，立即恢复可用）；幂等。"""
    await _load_tenant_key(db, key_id, tenant_id)
    key = await enable_api_key(db, redis, key_id=key_id)
    await db.commit()
    return success(ApiKeyOut.model_validate(key))


@router.post("/{key_id}/delete", response_model=Result[None], summary="删除本租户 API Key")
@audit_action(action="delete", resource="api_key", resource_id="key_id")
async def delete_tenant_key(
    key_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(TENANT_API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    tenant_id: int = Depends(current_tenant_id),
    user: SessionData = Depends(get_current_user),
):
    """彻底删除 Key（不可恢复）；DEL 缓存使网关即时 401。"""
    await _load_tenant_key(db, key_id, tenant_id)
    await delete_api_key(db, redis, key_id=key_id)
    await db.commit()
    return success(None)


@router.post("/{key_id}/reset-budget", response_model=Result[ApiKeyOut], summary="重置本租户 Key 预算")
@audit_action(action="update", resource="api_key", resource_id="key_id")
async def reset_tenant_key_budget(
    key_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(TENANT_API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    tenant_id: int = Depends(current_tenant_id),
    user: SessionData = Depends(get_current_user),
):
    """归零重置：``budget_spent`` 清 0 + Key 维度 token 配额计数器（Redis）清零
    （``budget_limit`` / ``total_token_quota`` 等上限不动），周期性预算重置用。"""
    await _load_tenant_key(db, key_id, tenant_id)
    key = await reset_budget(db, redis, key_id=key_id)
    await db.commit()
    return success(ApiKeyOut.model_validate(key))
