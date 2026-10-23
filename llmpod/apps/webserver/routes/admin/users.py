"""管理员用户管理路由（需要平台权限）。"""

from __future__ import annotations

import logging
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
from llmpod.config.constants import PLATFORM_TENANT_ID
from llmpod.core.exceptions import Conflict, LLMPodValidationError, NotFound
from llmpod.core.security import hash_password
from llmpod.db import get_db
from llmpod.models.role import Role
from llmpod.models.user import User
from llmpod.models.user_role import UserRole
from llmpod.apps.webserver.schemas.response import PaginatedResponse
from llmpod.apps.webserver.schemas.out_models import UserDetailOut
from llmpod.permission.permissions import (
    ADMIN_USER_CREATE,
    ADMIN_USER_DELETE,
    ADMIN_USER_READ,
    ADMIN_USER_UPDATE,
)
from llmpod.services.audit_service import audit_action
from llmpod.services.user_service import (
    assert_not_last_admin,
    email_unique_violation,
    enrich_users_role_ids,
    enrich_users_tenant_balance,
)

log = logging.getLogger("llmpod.apps.webserver.admin.users")

router = APIRouter()

# 用户状态合法值（与 User ORM 注释一致：active | disabled）
_USER_STATUSES = set(AccountStatus)


class CreateUserRequest(BaseModel):
    username: str = Field(..., max_length=64, description="用户名（全局唯一，登录用）")
    password: str = Field(..., description="登录密码（明文传入，服务端哈希存储）")
    email: str | None = Field(None, max_length=128, description="邮箱（租户内唯一，可空）")
    phone: str | None = Field(None, max_length=32, description="手机号，可空")
    role_ids: list[int] = Field(..., description="初始角色ID列表（去重写入，需为已存在的角色）")


class UpdateUserRequest(BaseModel):
    password: str | None = Field(None, description="新密码（明文传入，服务端哈希后覆盖）；不传则不改密")
    email: str | None = Field(None, max_length=128, description="邮箱（租户内唯一，可空）；不传或传空串则清空")
    phone: str | None = Field(None, max_length=32, description="手机号，可空；不传或传空串则清空")
    status: str | None = Field(None, description="用户状态：active | disabled；不传则不修改")


class UserDeleteData(BaseModel):
    """删除用户回执。"""

    id: int = Field(..., description="被删用户ID")
    deleted: bool = Field(True, description="删除是否生效")


@router.get(
    "",
    response_model=PaginatedResponse[UserDetailOut],
    summary="列出平台用户",
    dependencies=[Depends(require_permission(ADMIN_USER_READ))],
)
async def list_users(
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数，最大100"),
    db: AsyncSession = Depends(get_db),
):
    """列出平台（管理员端）用户（仅限平台管理员）。

    管理员端只看自己的：固定作用域 ``tenant_id == PLATFORM_TENANT_ID``，不暴露租户用户（租户用户由租户端 ``/tenant/users`` 自治管理）。
    """
    stmt = select(User).where(User.tenant_id == PLATFORM_TENANT_ID)
    count_stmt = select(func.count(User.id)).where(User.tenant_id == PLATFORM_TENANT_ID)

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = stmt.order_by(User.id.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    users = list(result.scalars())
    # 批量装配 role_ids + 所属租户余额（各一条 IN 查询，零 N+1），供前端回显
    users = await enrich_users_role_ids(db, users)
    users = await enrich_users_tenant_balance(db, users)

    return PaginatedResponse.result(items=users, total=total, page=page, page_size=page_size)


@router.post(
    "",
    response_model=Result[UserDetailOut],
    summary="创建平台用户",
    dependencies=[Depends(require_permission(ADMIN_USER_CREATE))],
)
@audit_action(action="create", resource="user", fixed_tenant_id=PLATFORM_TENANT_ID)
async def create_user(
    request: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """创建平台（管理员端）用户（仅限平台管理员）。

    固定归属平台租户（``tenant_id=PLATFORM_TENANT_ID``）——管理员端只管自己端的用户；租户用户由 ``/tenant/users`` 或创建租户时引导的超管管理。
    """
    new_user = User(
        username=request.username,
        password_hash=hash_password(request.password),
        email=request.email,
        phone=request.phone,
        tenant_id=PLATFORM_TENANT_ID,
    )
    db.add(new_user)
    try:
        await db.flush()
    except IntegrityError as e:
        # 并发场景下唯一约束兜底。该 INSERT 带两条唯一约束：uq_user_username（全局，
        # 跨租户/跨端共用）与 uq_user_tenant_email（租户内邮箱唯一）——按约束名分诊，
        # 邮箱撞了报邮箱，不然一律按用户名（全局某处已被占用）。
        await db.rollback()
        if email_unique_violation(e):
            raise Conflict("该邮箱在本租户内已被使用")
        raise Conflict("该用户名不可用")

    # 去重避免同一 role_id 重复插入
    seen: set[int] = set()
    for rid in request.role_ids:
        if rid in seen:
            continue
        seen.add(rid)

    # 语义固化：平台用户只能绑平台域角色（tenant_id=0）。UserRole 无物理 FK，
    if seen:
        valid_ids = {
            rid
            for (rid,) in (
                await db.execute(
                    select(Role.id).where(
                        Role.id.in_(seen),
                        Role.tenant_id == PLATFORM_TENANT_ID,
                    )
                )
            ).all()
        }
        bad = seen - valid_ids
        if bad:
            await db.rollback()
            raise Conflict(
                f"仅可分配平台域（tenant_id={PLATFORM_TENANT_ID}）角色，以下 role_id 不存在或非平台域：{sorted(bad)}"
            )

    for rid in seen:
        db.add(UserRole(user_id=new_user.id, role_id=rid))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("角色 ID 无效或已分配")

    await db.commit()
    # role_ids 刚写入（见上方 seen），直接复用，避免再发一次 IN 查询回查
    new_user.role_ids = sorted(seen)
    return success(new_user)


@router.get(
    "/{user_id}",
    response_model=Result[UserDetailOut],
    summary="获取用户详情",
    dependencies=[Depends(require_permission(ADMIN_USER_READ))],
)
async def get_user(
    user_id: Annotated[int, Path(description="用户ID")],
    db: AsyncSession = Depends(get_db),
):
    """根据 ID 获取平台用户（管理员端只管 tenant_id=0 的用户）。"""
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if not user:
        raise NotFound("User", user_id)
    await enrich_users_role_ids(db, [user])
    return success(user)


@router.post(
    "/{user_id}/update",
    response_model=Result[UserDetailOut],
    summary="更新用户",
    dependencies=[Depends(require_permission(ADMIN_USER_UPDATE))],
)
@audit_action(action="update", resource="user", resource_id="user_id", fixed_tenant_id=PLATFORM_TENANT_ID)
async def update_user(
    user_id: Annotated[int, Path(description="用户ID")],
    request: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新用户资料 / 状态 / 重置密码（角色管理走 /admin/access）。

    username 与 tenant_id 不可改（重命名 / 跨租户迁移不在本接口范围）。
    """
    updates = request.model_dump(exclude_none=True)
    # tenant 端没有这条守卫：该端更新表单只带 password/email/phone，空请求是「清空两个可选资料字段」的
    # 合法表达；本端表单必带 status，空请求只可能来自裸调 API，故仍拒。
    if not updates:
        raise LLMPodValidationError("未提供任何待更新字段")
    if "status" in updates and updates["status"] not in _USER_STATUSES:
        raise LLMPodValidationError(f"非法用户状态：{updates['status']}")

    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if not user:
        raise NotFound("User", user_id)

    if "password" in updates:
        user.password_hash = hash_password(updates.pop("password"))
    # email/phone 缺席即清空（前端清空输入框时不下发该键，空串一并收口成 NULL），故不走 `in updates`。
    # 代价：列表页启停只发 {status}，与该表单清空两个字段时同形 → 启停会一并清空这两列。
    user.email = request.email or None
    user.phone = request.phone or None
    if "status" in updates:
        user.status = updates["status"]

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("该邮箱在本租户内已被使用")
    # 改密/停用即时吊销该用户全部存量会话（会话版本 bump；只动资料时不吊销）
    if request.password is not None or request.status is not None:
        from llmpod.services import session_version_service

        await session_version_service.bump_version(user.id, db)
    # flush 不带回 onupdate 生成的 updated_at，序列化前同步 refresh 加载，避免 MissingGreenlet → 500
    await db.refresh(user)
    await db.commit()
    await enrich_users_role_ids(db, [user])
    return success(user)


@router.post(
    "/{user_id}/delete",
    response_model=Result[UserDeleteData],
    summary="删除用户",
    dependencies=[Depends(require_permission(ADMIN_USER_DELETE))],
)
@audit_action(action="delete", resource="user", resource_id="user_id", fixed_tenant_id=PLATFORM_TENANT_ID)
async def delete_user(
    user_id: Annotated[int, Path(description="用户ID")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """删除平台用户（管理员端，级联清理其角色关联）。

    守卫：若是某管理员角色的最后一名持有者则拒绝（防止"无主"，见 ``assert_not_last_admin``）。
    """
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if not user:
        raise NotFound("User", user_id)

    await assert_not_last_admin(db, user_id)

    # 级联清理角色关联（逻辑外键，无 DB 级联）
    await db.execute(delete(UserRole).where(UserRole.user_id == user_id))
    await db.delete(user)
    await db.flush()
    await db.commit()
    # 鉴权查数据库用户行；删除提交后旧会话即失效。
    return success(UserDeleteData(id=user_id))
