"""管理员 API Key 管理路由（网关鉴权依据的签发/列表/禁用/启用/删除/调整）。

仅 GET/POST：写操作一律 POST 动作路径（/{id}/update），不使用 PATCH/PUT/DELETE。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.schemas.request import INT32_MAX, INT64_MAX, NUMERIC_20_6_MAX
from llmpod.apps.webserver.schemas.out_models import ApiKeyCreatedOut, ApiKeyOut
from llmpod.db import get_db, get_redis
from llmpod.services.tenant_scope import platform_scope_requested
from llmpod.permission.permissions import API_KEY_CREATE, API_KEY_READ, API_KEY_UPDATE
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

log = logging.getLogger("llmpod.apps.webserver.admin.api_keys")

router = APIRouter()


class CreateApiKeyRequest(BaseModel):
    tenant_id: int | None = Field(
        None, ge=1, description="所属租户ID（空=平台直发 Key，跳过租户授权校验；0 不是合法值）"
    )
    name: str = Field(..., min_length=1, max_length=128, description="Key 显示名")
    allowed_models: list[str] = Field(
        default_factory=list, description="允许调用的模型别名列表（缺省/空=全部模型可用）"
    )
    rpm_limit: int | None = Field(None, gt=0, le=INT32_MAX, description="每分钟请求数上限（空=不限）")
    tpm_limit: int | None = Field(None, gt=0, le=INT32_MAX, description="每分钟 token 上限（空=不限）")
    total_token_quota: int | None = Field(None, gt=0, le=INT64_MAX, description="Key 总 token 配额（空=不限）")
    model_token_quotas: dict[str, int] | None = Field(None, description="按模型的 token 配额映射")
    budget_limit: Decimal | None = Field(None, ge=0, le=NUMERIC_20_6_MAX, description="Key 预算上限（元，空=不限）")
    expires_at: datetime | None = Field(None, description="过期时间（空=永不过期）")


class UpdateApiKeyRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128, description="不传则不修改")
    allowed_models: list[str] | None = Field(
        None, description="不传则不修改（重新走可用模型校验；传空列表=清空为全部模型可用）"
    )
    rpm_limit: int | None = Field(None, gt=0, le=INT32_MAX, description="不传则不修改；传 null=清空（不限）")
    tpm_limit: int | None = Field(None, gt=0, le=INT32_MAX, description="不传则不修改；传 null=清空（不限）")
    total_token_quota: int | None = Field(None, gt=0, le=INT64_MAX, description="不传则不修改；传 null=清空（不限）")
    model_token_quotas: dict[str, int] | None = Field(None, description="不传则不修改")
    budget_limit: Decimal | None = Field(
        None, ge=0, le=NUMERIC_20_6_MAX, description="不传则不修改；传 null=清空（不限）"
    )
    expires_at: datetime | None = Field(None, description="不传则不修改；传 null=清空（永不过期）")


@router.post("", response_model=Result[ApiKeyCreatedOut], summary="签发 API Key")
@audit_action(
    action="create",
    resource="api_key",
    resource_id=lambda r: r["data"]["api_key"]["id"],
    tenant_id_resolver=lambda r: r["data"]["api_key"]["tenant_id"],
)
async def create_key(
    body: CreateApiKeyRequest,
    _perm: None = Depends(require_permission(API_KEY_CREATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: SessionData = Depends(get_current_user),
):
    """签发新的 API 网关 Key。

    明文可逆加密持久存储（管理端内部解密使用），鉴权用 SHA-256 哈希；
    ``plaintext`` 字段直接回显完整明文（凭据材料口径，与列表一致）。
    ``tenant_id`` 为空签发的是平台直发 Key（网关跳过租户封禁与扣费），
    指定租户则 Key 归属该租户，``allowed_models`` 需 ⊆ 该租户可用模型（已发布且未被平台封禁）。
    """
    key, plaintext = await create_api_key(
        db,
        redis,
        tenant_id=body.tenant_id,
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


@router.get("", response_model=Result[list[ApiKeyOut]], summary="列出 API Key（平台域，默认不含租户 Key）")
async def list_keys(
    _perm: None = Depends(require_permission(API_KEY_READ)),
    db: AsyncSession = Depends(get_db),
    tenant_id: int | None = Query(None, description="按所属租户过滤（不传或传 0=平台直发 Key，不含租户 Key）"),
    name: str | None = Query(None, description="按显示名模糊过滤（contains）"),
    status: str | None = Query(None, description="按状态过滤：active | disabled"),
):
    """列出平台直发 Key（``tenant_id`` 为空，按 id 倒序）；返回元数据 + 完整明文。

    筛选与租户端 ``/tenant/api-keys`` 同口径（name 模糊 / status 精确）；
    不传或传 0 = 平台域（与审计日志同口径），要看某个具体租户的 Key 须显式传
    ``tenant_id>=1``。
    """
    keys = await list_api_keys(
        db,
        tenant_id=tenant_id,
        name=name,
        status=status,
        exclude_tenant_scope=platform_scope_requested(tenant_id, exclude_tenant_scope=True),
    )
    return success([ApiKeyOut.model_validate(k) for k in keys])


@router.post("/{key_id}/disable", response_model=Result[ApiKeyOut], summary="禁用 API Key")
@audit_action(
    action="update", resource="api_key", resource_id="key_id", tenant_id_resolver=lambda r: r["data"]["tenant_id"]
)
async def disable_key(
    key_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: SessionData = Depends(get_current_user),
):
    """禁用后立即失效：置 status=disabled 并 DEL Redis 缓存（网关下次请求即 401）；幂等。"""
    key = await disable_api_key(db, redis, key_id=key_id)
    await db.commit()
    return success(ApiKeyOut.model_validate(key))


@router.post("/{key_id}/enable", response_model=Result[ApiKeyOut], summary="启用 API Key")
@audit_action(
    action="update", resource="api_key", resource_id="key_id", tenant_id_resolver=lambda r: r["data"]["tenant_id"]
)
async def enable_key(
    key_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: SessionData = Depends(get_current_user),
):
    """重新启用被禁用的 Key：置 status=active 并 DEL 缓存（立即恢复可用）；幂等。"""
    key = await enable_api_key(db, redis, key_id=key_id)
    await db.commit()
    return success(ApiKeyOut.model_validate(key))


@router.post("/{key_id}/delete", response_model=Result[None], summary="删除 API Key")
@audit_action(action="delete", resource="api_key", resource_id="key_id")
async def delete_key(
    key_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: SessionData = Depends(get_current_user),
):
    """彻底删除 Key（不可恢复，区别于 disable 的保留记录）；DEL 缓存使网关即时 401。"""
    await delete_api_key(db, redis, key_id=key_id)
    await db.commit()
    return success(None)


@router.post("/{key_id}/update", response_model=Result[ApiKeyOut], summary="更新 API Key")
@audit_action(
    action="update",
    resource="api_key",
    resource_id="key_id",
    tenant_id_resolver=lambda r: r["data"]["tenant_id"],
    params_exclude_unset=True,  # 三态更新：显式 null=清空须与「未传」在审计里可区分
)
async def update_key(
    key_id: Annotated[int, Path()],
    body: UpdateApiKeyRequest,
    _perm: None = Depends(require_permission(API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: SessionData = Depends(get_current_user),
):
    """更新 Key 元数据（显示名 / 可用模型 / 限流 / 配额 / 预算 / 过期时间）。

    ``allowed_models`` 变更重新走可用模型校验（租户 Key 需 ⊆ 该租户可用模型）；
    更新后同步失效 Redis 缓存使新限制即时生效。
    可空字段（限流/配额/预算/过期时间）三态：不传=不改，传 null=清空（不限/永不过期）。
    """
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


@router.post("/{key_id}/reset-budget", response_model=Result[ApiKeyOut], summary="重置 Key 预算")
@audit_action(
    action="update", resource="api_key", resource_id="key_id", tenant_id_resolver=lambda r: r["data"]["tenant_id"]
)
async def reset_key_budget(
    key_id: Annotated[int, Path()],
    _perm: None = Depends(require_permission(API_KEY_UPDATE)),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
    user: SessionData = Depends(get_current_user),
):
    """归零重置：``budget_spent`` 清 0 + Key 维度 token 配额计数器（Redis）清零
    （``budget_limit`` / ``total_token_quota`` 等上限不动），周期性预算重置用。"""
    key = await reset_budget(db, redis, key_id=key_id)
    await db.commit()
    return success(ApiKeyOut.model_validate(key))
