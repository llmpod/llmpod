"""管理员租户管理路由（需要平台权限）。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success
from llmpod.apps.webserver.constants import AccountStatus
from llmpod.config.constants import PLATFORM_TENANT_ID, TENANT_SUPER_ADMIN_ROLE_CODE
from llmpod.core.exceptions import Conflict, LLMPodError, LLMPodValidationError, NotFound
from llmpod.core.security import hash_password
from llmpod.db import get_db
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.models.tenant import Tenant
from llmpod.models.tenant_model_block import TenantModelBlock
from llmpod.models.user import User
from llmpod.models.user_role import UserRole
from llmpod.apps.webserver.schemas.response import PaginatedResponse
from llmpod.apps.webserver.schemas.out_models import TenantDetailOut
from llmpod.permission.permissions import (
    PLATFORM_TENANT_CREATE,
    PLATFORM_TENANT_DELETE,
    PLATFORM_TENANT_READ,
    PLATFORM_TENANT_UPDATE,
)
from llmpod.services.audit_service import audit_action
from llmpod.services.tenant_service import enrich_tenants, tenant_overview
from llmpod.services.model_block_service import set_tenant_model_blocks

log = logging.getLogger("llmpod.apps.webserver.admin.tenants")

router = APIRouter()

# 租户状态合法值（与 Tenant ORM 注释一致：active | disabled）
_TENANT_STATUSES = set(AccountStatus)


class CreateTenantRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="租户名称（唯一）")
    admin_username: str = Field(..., max_length=64, description="租户超级管理员用户名（全局唯一，登录用）")
    admin_password: str = Field(..., description="租户超级管理员登录密码（明文传入，服务端哈希存储）")
    admin_email: str | None = Field(None, max_length=128, description="租户超级管理员邮箱（租户内唯一，可空）")
    admin_phone: str | None = Field(None, max_length=32, description="租户超级管理员手机号，可空")
    blocked_route_ids: list[int] | None = Field(
        None, description="封禁的路由ID列表（可选；默认放行全部已发布路由，此列表为封禁例外）"
    )


class UpdateTenantRequest(BaseModel):
    name: str | None = Field(None, description="租户名称（唯一）；不传则不修改")
    status: str | None = Field(None, description="租户状态：active | disabled；不传则不修改")
    blocked_route_ids: list[int] | None = Field(
        None, description="封禁的路由ID列表（全量替换语义；不传则不修改，传空列表=清空封禁）"
    )


class TenantDeleteData(BaseModel):
    """删除租户回执（级联清该租户用户/角色及授权关联）。"""

    id: int = Field(..., description="被删租户ID")
    deleted: bool = Field(True, description="删除是否生效")


class TenantOverviewTrendOut(BaseModel):
    """租户概览·消费趋势系列（单租户）。"""

    tenant_id: int = Field(..., description="租户ID")
    tenant_name: str = Field(..., description="租户名称")
    data: list[float] = Field(..., description="逐日消费额（元，与共享 X 轴 dates 对位；空日为 0）")


class TenantOverviewBalanceOut(BaseModel):
    """租户概览·余额 Top 行。"""

    tenant_id: int = Field(..., description="租户ID")
    tenant_name: str = Field(..., description="租户名称")
    balance: Decimal = Field(..., description="总余额（元）")
    available: Decimal = Field(..., description="可用余额（元，= balance - frozen_balance）")


class TenantOverviewOut(BaseModel):
    """租户概览页整屏聚合（单请求）。"""

    total_tenants: int = Field(..., description="租户总数（不含平台租户）")
    active_tenants: int = Field(..., description="正常租户数（status=active）")
    disabled_tenants: int = Field(..., description="禁用租户数（status=disabled）")
    total_users: int = Field(..., description="租户用户总数（平台用户不计）")
    dates: list[str] = Field(..., description="消费趋势共享 X 轴（近 days 天 ISO 日期）")
    consume_trend: list[TenantOverviewTrendOut] = Field(..., description="消费趋势 top_n（按窗口消费总额降序）")
    balance_top: list[TenantOverviewBalanceOut] = Field(..., description="余额 top_n（按可用余额降序）")


@router.get(
    "",
    response_model=PaginatedResponse[TenantDetailOut],
    summary="列出租户",
    dependencies=[Depends(require_permission(PLATFORM_TENANT_READ))],
)
async def list_tenants(
    status: str | None = Query(None, description="租户状态"),
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，最大100"),
    db: AsyncSession = Depends(get_db),
):
    """列出所有租户（仅限平台管理员）。"""
    stmt = select(Tenant)
    count_stmt = select(func.count(Tenant.id))

    if status:
        stmt = stmt.where(Tenant.status == status)
        count_stmt = count_stmt.where(Tenant.status == status)

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = stmt.order_by(Tenant.id).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    tenants = list(result.scalars())
    # 批量装配 user_count / role_count（各一条 GROUP BY，零 N+1）
    tenants = await enrich_tenants(db, tenants)

    return PaginatedResponse.result(items=tenants, total=total, page=page, page_size=page_size)


@router.get(
    "/overview",
    response_model=Result[TenantOverviewOut],
    summary="租户概览整屏聚合（统计卡 + 消费趋势 + 余额 Top）",
    dependencies=[Depends(require_permission(PLATFORM_TENANT_READ))],
)
async def get_tenant_overview(
    days: Annotated[int, Query(ge=1, le=90, description="消费趋势窗口天数")] = 14,
    top_n: Annotated[int, Query(ge=1, le=20, description="趋势/余额各取前 N 名租户")] = 5,
    db: AsyncSession = Depends(get_db),
):
    """租户概览页一次取整屏（"冻结/预逾期"口径 = status=disabled，无套餐/到期概念）。"""
    data = await tenant_overview(db, days=days, top_n=top_n)
    return success(TenantOverviewOut(**data))


@router.post(
    "",
    response_model=Result[TenantDetailOut],
    summary="创建租户",
    dependencies=[Depends(require_permission(PLATFORM_TENANT_CREATE))],
)
@audit_action(action="create", resource="tenant", tenant_id_resolver=lambda r: getattr(r.get("data"), "id", None))
async def create_tenant(
    request: CreateTenantRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """创建新租户并引导租户超级管理员（仅限平台管理员）。

    流程：
    1. 建租户行（name 唯一）；
    2. 在该租户下 upsert 内置租户角色（tenant_admin / tenant_operator）并按
       ``llmpod.permission.tenant.ROLE_PERMISSIONS`` 授权（幂等）；
    3. 写死绑定 ``TENANT_SUPER_ADMIN_ROLE_CODE``（= tenant_admin）创建租户超管用户，
       使新租户开通即可用本租户超管登录管理。

    角色不外暴露：前端无需选角色，超管角色由本接口按 code 固定。
    """
    tenant = Tenant(name=request.name)
    db.add(tenant)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("该租户名已存在")

    # 在该租户下 upsert 内置租户角色并授权（幂等）
    from llmpod.permission.seed import seed_tenant_builtin_roles

    await seed_tenant_builtin_roles(db, tenant.id)

    # 引导租户超级管理员：写死绑 TENANT_SUPER_ADMIN_ROLE_CODE 角色
    admin_role = (
        await db.execute(
            select(Role).where(
                Role.tenant_id == tenant.id,
                Role.code == TENANT_SUPER_ADMIN_ROLE_CODE,
            )
        )
    ).scalar_one_or_none()
    if admin_role is None:
        await db.rollback()
        raise LLMPodError(f"租户内置角色 {TENANT_SUPER_ADMIN_ROLE_CODE} 未创建，无法引导超管")

    admin_user = User(
        username=request.admin_username,
        password_hash=hash_password(request.admin_password),
        email=request.admin_email,
        phone=request.admin_phone,
        tenant_id=tenant.id,
    )
    db.add(admin_user)
    try:
        await db.flush()
    except IntegrityError:
        # (username 全局唯一) / (tenant_id, email) 唯一兜底
        await db.rollback()
        raise Conflict("管理员用户名或邮箱已存在")
    db.add(UserRole(user_id=admin_user.id, role_id=admin_role.id))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("绑定租户超级管理员角色失败")

    if request.blocked_route_ids:
        await set_tenant_model_blocks(db, tenant_id=tenant.id, route_ids=request.blocked_route_ids)

    await db.commit()
    # 装配统计（新建租户已 seed 内置角色 + 1 超管，role_count ≥ 内置角色数、user_count ≥ 1）
    await enrich_tenants(db, [tenant])
    return success(tenant)


@router.get(
    "/{tenant_id}",
    response_model=Result[TenantDetailOut],
    summary="获取租户详情",
    dependencies=[Depends(require_permission(PLATFORM_TENANT_READ))],
)
async def get_tenant(
    tenant_id: Annotated[int, Path(description="租户ID")],
    db: AsyncSession = Depends(get_db),
):
    """根据 ID 获取租户。"""
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise NotFound("Tenant", tenant_id)
    await enrich_tenants(db, [tenant])
    return success(tenant)


@router.post(
    "/{tenant_id}/update",
    response_model=Result[TenantDetailOut],
    summary="更新租户",
    dependencies=[Depends(require_permission(PLATFORM_TENANT_UPDATE))],
)
@audit_action(action="update", resource="tenant", resource_id="tenant_id", tenant_id_arg="tenant_id")
async def update_tenant(
    tenant_id: Annotated[int, Path(description="租户ID")],
    request: UpdateTenantRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新租户名称 / 状态（返回更新后的租户）。"""
    updates = request.model_dump(exclude_none=True)
    blocked_route_ids = updates.pop("blocked_route_ids", None)  # None = 不传不改
    if not updates and blocked_route_ids is None:
        raise LLMPodValidationError("未提供任何待更新字段")
    if "status" in updates and updates["status"] not in _TENANT_STATUSES:
        raise LLMPodValidationError(f"非法租户状态：{updates['status']}")

    # 平台租户不可禁用（会扰乱平台域 RBAC）
    if tenant_id == PLATFORM_TENANT_ID and updates.get("status") == AccountStatus.DISABLED:
        raise LLMPodError(f"平台租户(id={PLATFORM_TENANT_ID})不可禁用")

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise NotFound("Tenant", tenant_id)

    # 逐字段显式赋值（exclude_none 已滤 None；新字段加这里，不走键名循环）
    if "name" in updates:
        tenant.name = updates["name"]
    if "status" in updates:
        tenant.status = updates["status"]
    if blocked_route_ids is not None:
        await set_tenant_model_blocks(db, tenant_id=tenant_id, route_ids=blocked_route_ids)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("该租户名已存在")
    # flush 不带回 onupdate 生成的 updated_at，序列化前同步 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(tenant)
    await db.commit()
    await enrich_tenants(db, [tenant])
    return success(tenant)


@router.post(
    "/{tenant_id}/delete",
    response_model=Result[TenantDeleteData],
    summary="删除租户",
    dependencies=[Depends(require_permission(PLATFORM_TENANT_DELETE))],
)
@audit_action(action="delete", resource="tenant", resource_id="tenant_id", tenant_id_arg="tenant_id")
async def delete_tenant(
    tenant_id: Annotated[int, Path(description="租户ID")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """删除租户。

    守卫：
    - 平台租户（id=0）不可删除（亦由 ORM before_delete 兜底）。

    级联：清理该租户下用户的 UserRole 关联与用户记录、角色的 RolePermission /
    UserRole 关联与角色记录（零物理 FK）。创建租户时强制引导超管，故租户必有用户，
    删除一并清理之（不再因"有用户"而拒绝）。
    """
    if tenant_id == PLATFORM_TENANT_ID:
        raise LLMPodError(f"平台租户(id={PLATFORM_TENANT_ID})不可删除")

    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if not tenant:
        raise NotFound("Tenant", tenant_id)

    # 该租户下的全部用户 id（先回收其 UserRole，再删用户）
    user_ids_stmt = select(User.id).where(User.tenant_id == tenant_id)
    user_ids = [uid for (uid,) in (await db.execute(user_ids_stmt)).all()]
    if user_ids:
        await db.execute(delete(UserRole).where(UserRole.user_id.in_(user_ids)))
        await db.execute(delete(User).where(User.id.in_(user_ids)))

    # 级联清理该租户的角色及其授权关联（逻辑外键，无 DB 级联）
    role_ids_stmt = select(Role.id).where(Role.tenant_id == tenant_id)
    role_ids = [rid for (rid,) in (await db.execute(role_ids_stmt)).all()]
    if role_ids:
        await db.execute(delete(RolePermission).where(RolePermission.role_id.in_(role_ids)))
        await db.execute(delete(UserRole).where(UserRole.role_id.in_(role_ids)))
        await db.execute(delete(Role).where(Role.tenant_id == tenant_id))

    # 清理该租户的模型封禁行（逻辑外键无级联）
    await db.execute(delete(TenantModelBlock).where(TenantModelBlock.tenant_id == tenant_id))

    await db.delete(tenant)
    await db.flush()
    await db.commit()
    # 用户行已删；鉴权查库即可拒绝全部旧会话。
    return success(TenantDeleteData(id=tenant_id))
