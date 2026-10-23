"""权限管理路由：权限、角色、角色-权限、用户-角色。

权限是 ``scope:resource:action`` 三元组；角色通过 ``RolePermission`` 绑权限，
用户通过 ``UserRole`` 绑角色。本路由示范"外层门禁 + 内层权限校验"两种用法：
- router 顶部 ``require_permission(ACCESS_READ)``：所有端点先过 access:read 外层门禁；
- 写操作 endpoint 再单独挂 ``require_permission(ACCESS_UPDATE)`` 做内层权限校验。

``/admin`` 顶部已挂 ``require_platform_role``，故此处不再重复 access_domain 门。
权限选择器（``list_permissions``）默认只列 ``scope=platform``，可经 ``?scope=`` 切域，
使管理端不暴露租户端权限、租户端不暴露平台端权限（按域隔离 RBAC 管理视图）。

**平台域作用域**：本路由全部端点固定操作平台租户（``tenant_id == PLATFORM_TENANT_ID``）的角色与用户——管理员端只管平台域自己的角色/用户/授权，不暴露也不可变更租户端的角色与用户（租户端自治管理见 ``routes/tenant/access.py``）。所有角色/用户查询与写操作均强制按平台域 ``tenant_id`` 过滤。
全系统统一只用 GET / POST：写操作（创建/替换/回收）一律走 POST；回收类操作在
资源路径后追加 ``/revoke`` 动作段以区别于创建/赋权，避免与 POST 集合端点冲突。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import get_current_user, require_permission
from llmpod.apps.webserver.auth.session import SessionData
from llmpod.apps.webserver.schemas import Result, success, PermissionOut, RoleDetailOut
from llmpod.config.constants import ADMIN_ROLE_CODES, PLATFORM_TENANT_ID
from llmpod.core.exceptions import Conflict, LLMPodError, LLMPodValidationError, NotFound
from llmpod.db import get_db
from llmpod.models.permission import Permission
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.models.user import User
from llmpod.models.user_role import UserRole
from llmpod.permission import SCOPE_PLATFORM
from llmpod.permission.permissions import ACCESS_READ, ACCESS_UPDATE
from llmpod.services.audit_service import audit_action
from llmpod.services.permission_service import (
    enrich_roles_permission_ids,
    get_role_permissions,
    get_user_permissions,
)

log = logging.getLogger("llmpod.apps.webserver.routes.admin.access")

router = APIRouter(
    # 外层门禁：访问权限管理接口需先持有 access:read
    dependencies=[Depends(require_permission(ACCESS_READ))]
)


# ---- 权限 ----


@router.get("/permissions", response_model=Result[list[PermissionOut]], summary="列出权限")
async def list_permissions(
    scope: str | None = Query(None, description="按域过滤（platform|tenant）；不传默认只列 platform"),
    db: AsyncSession = Depends(get_db),
):
    """列出权限（按 scope, resource, action 排序）。

    默认只列 ``scope=platform``，避免管理端暴露租户端权限噪音；显式传 ``?scope=tenant``
    可查看租户侧权限。
    """
    effective_scope = scope if scope is not None else SCOPE_PLATFORM
    stmt = (
        select(Permission).where(Permission.scope == effective_scope).order_by(Permission.resource, Permission.action)
    )
    result = await db.execute(stmt)
    perms = list(result.scalars())
    return success(perms)


# ---- 角色 ----


class CreateRoleRequest(BaseModel):
    name: str = Field(..., description="角色名称（展示用）")


class UpdateRoleRequest(BaseModel):
    name: str | None = Field(None, description="角色名称（展示用）；不传则不修改")


class RoleDeleteData(BaseModel):
    """删除角色回执。"""

    id: int = Field(..., description="被删角色ID")
    deleted: bool = Field(True, description="删除是否生效")


@router.get("/roles", response_model=Result[list[RoleDetailOut]], summary="列出平台域角色")
async def list_roles(
    db: AsyncSession = Depends(get_db),
):
    """列出平台域角色（固定 ``tenant_id == PLATFORM_TENANT_ID``）。

    管理员端只看自己域的角色，不暴露租户端角色（租户角色由租户端
    ``/tenant/access/roles`` 自治管理）。
    """
    stmt = select(Role).where(Role.tenant_id == PLATFORM_TENANT_ID).order_by(Role.id)
    result = await db.execute(stmt)
    roles = list(result.scalars())
    # 批量装配 permission_ids（一条 IN 查询，零 N+1），供前端回显角色已授权权限
    roles = await enrich_roles_permission_ids(db, roles)
    return success(roles)


@router.post(
    "/roles",
    response_model=Result[RoleDetailOut],
    summary="创建平台域自定义角色",
    dependencies=[Depends(require_permission(ACCESS_UPDATE))],
)
@audit_action(action="create", resource="access", fixed_tenant_id=PLATFORM_TENANT_ID)
async def create_role(
    request: CreateRoleRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """创建平台域自定义角色（``is_builtin=False``，固定 ``tenant_id=PLATFORM_TENANT_ID``）。"""
    role = Role(
        tenant_id=PLATFORM_TENANT_ID,
        name=request.name,
        is_builtin=False,
    )
    db.add(role)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("该角色名已存在")
    await db.commit()
    # 新建角色尚无授权，permission_ids 必为空，直接置默认，避免空 IN 查询
    role.permission_ids = []
    return success(role)


@router.post(
    "/roles/{role_id}/update",
    response_model=Result[RoleDetailOut],
    summary="更新平台域角色",
    dependencies=[Depends(require_permission(ACCESS_UPDATE))],
)
@audit_action(action="update", resource="access", resource_id="role_id", fixed_tenant_id=PLATFORM_TENANT_ID)
async def update_role(
    role_id: Annotated[int, Path(description="角色ID")],
    request: UpdateRoleRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """更新平台域角色名称（code 为角色身份标识，不可改）。"""
    if not request.name:
        raise LLMPodValidationError("未提供任何待更新字段")

    role = (
        await db.execute(
            select(Role).where(
                Role.id == role_id,
                Role.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFound("Role", role_id)
    role.name = request.name
    await db.flush()
    await db.commit()
    await enrich_roles_permission_ids(db, [role])
    return success(role)


@router.post(
    "/roles/{role_id}/delete",
    response_model=Result[RoleDeleteData],
    summary="删除平台域角色",
    dependencies=[Depends(require_permission(ACCESS_UPDATE))],
)
@audit_action(action="delete", resource="access", resource_id="role_id", fixed_tenant_id=PLATFORM_TENANT_ID)
async def delete_role(
    role_id: Annotated[int, Path(description="角色ID")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """删除平台域自定义角色（级联清理 RolePermission / UserRole 关联）。

    守卫：
    - 仅可删平台域角色（``tenant_id == PLATFORM_TENANT_ID``）；
    - 内置角色（``is_builtin=True``）不可删除——其为系统/租户开通时自动建
    """
    role = (
        await db.execute(
            select(Role).where(
                Role.id == role_id,
                Role.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFound("Role", role_id)
    if role.is_builtin:
        raise Conflict("内置角色不可删除")

    affected_users = [
        uid for (uid,) in (await db.execute(select(UserRole.user_id).where(UserRole.role_id == role_id))).all()
    ]
    # 级联清理授权关联（逻辑外键，无 DB 级联）
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
    await db.execute(delete(UserRole).where(UserRole.role_id == role_id))
    await db.delete(role)
    await db.flush()
    from llmpod.services import session_version_service

    for uid in affected_users:
        await session_version_service.bump_version(uid, db)
    await db.commit()
    return success(RoleDeleteData(id=role_id))


# ---- 角色-权限 ----


class ReplaceRolePermissionsRequest(BaseModel):
    permission_ids: list[int] = Field(..., description="目标权限ID全集（全量替换，去重写入）")


class RolePermissionsData(BaseModel):
    """角色权限全量替换回执。"""

    role_id: int = Field(..., description="角色ID")
    permission_ids: list[int] = Field(default_factory=list, description="替换后的权限ID列表")


class PermissionRevokeData(BaseModel):
    """回收角色单个权限回执。"""

    role_id: int = Field(..., description="角色ID")
    permission_id: int = Field(..., description="权限ID")
    revoked: bool = Field(True, description="撤销是否生效")


@router.get("/roles/{role_id}/permissions", response_model=Result[list[str]], summary="列出角色已授权权限")
async def list_role_permissions(
    role_id: Annotated[int, Path(description="角色ID")],
    db: AsyncSession = Depends(get_db),
):
    """列出平台域角色已授权的权限串列表（scope:resource:action）。"""
    # 作用域守卫：仅平台域角色可见其授权
    role = (
        await db.execute(
            select(Role).where(
                Role.id == role_id,
                Role.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFound("Role", role_id)
    perms = await get_role_permissions(db, role_id)
    return success(perms)


@router.post(
    "/roles/{role_id}/permissions",
    response_model=Result[RolePermissionsData],
    summary="全量替换角色权限",
    dependencies=[Depends(require_permission(ACCESS_UPDATE))],
)
@audit_action(action="update", resource="access", resource_id="role_id", fixed_tenant_id=PLATFORM_TENANT_ID)
async def replace_role_permissions(
    role_id: Annotated[int, Path(description="角色ID")],
    request: ReplaceRolePermissionsRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """全量替换平台域角色权限集（body.permission_ids 即目标全集）。"""
    # 角色存在性 + 平台域作用域校验
    role = (
        await db.execute(
            select(Role).where(
                Role.id == role_id,
                Role.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFound("Role", role_id)

    # 写入前校验（去重）。role_permission 无物理 FK，有效性须在此显式校验：
    # 权限 ID 必须真实存在且属平台域（scope=platform），否则租户域/编造 ID 会静默落库
    seen: set[int] = set()
    for pid in request.permission_ids:
        if pid in seen:
            continue
        seen.add(pid)
    perms = list(
        (
            await db.execute(select(Permission).where(Permission.id.in_(seen), Permission.scope == SCOPE_PLATFORM))
        ).scalars()
    )
    if {p.id for p in perms} != seen:
        raise Conflict("存在无效或不属于平台域的权限 ID")
    # 校验通过才清空旧关联、写入新集合
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
    for pid in sorted(seen):
        db.add(RolePermission(role_id=role_id, permission_id=pid))
    await db.flush()
    await db.commit()
    return success(RolePermissionsData(role_id=role_id, permission_ids=sorted(seen)))


@router.post(
    "/roles/{role_id}/permissions/{permission_id}/revoke",
    response_model=Result[PermissionRevokeData],
    summary="回收角色单项权限",
    dependencies=[Depends(require_permission(ACCESS_UPDATE))],
)
@audit_action(action="update", resource="access", resource_id="role_id", fixed_tenant_id=PLATFORM_TENANT_ID)
async def revoke_role_permission(
    role_id: Annotated[int, Path(description="角色ID")],
    permission_id: Annotated[int, Path(description="权限ID")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """从平台域角色回收单个权限。"""
    # 作用域守卫：仅可操作平台域角色
    role = (
        await db.execute(
            select(Role).where(
                Role.id == role_id,
                Role.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFound("Role", role_id)
    await db.execute(
        delete(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == permission_id,
        )
    )
    await db.flush()
    await db.commit()
    return success(PermissionRevokeData(role_id=role_id, permission_id=permission_id))


# ---- 用户-角色 / 用户权限 ----


class AssignUserRolesRequest(BaseModel):
    role_ids: list[int] = Field(..., description="待追加的角色ID列表（幂等，已持有则不重复插入）")


class RolesAssignData(BaseModel):
    """给用户授予角色回执。"""

    user_id: int = Field(..., description="用户ID")
    added: list[int] = Field(default_factory=list, description="新授予的角色ID列表（已持有的跳过）")


class UserRoleRevokeData(BaseModel):
    """回收用户单个角色回执。"""

    user_id: int = Field(..., description="用户ID")
    role_id: int = Field(..., description="角色ID")
    revoked: bool = Field(True, description="撤销是否生效")


@router.get("/users/{user_id}/permissions", response_model=Result[list[str]], summary="用户有效权限集")
async def list_user_permissions(
    user_id: Annotated[int, Path(description="用户ID")],
    db: AsyncSession = Depends(get_db),
):
    """列出平台域用户在所有角色上的权限并集（scope:resource:action）。"""
    # 作用域守卫：仅平台域用户可见其权限
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("User", user_id)
    perms = await get_user_permissions(db, user_id)
    return success(sorted(perms))


@router.post(
    "/users/{user_id}/roles",
    response_model=Result[RolesAssignData],
    summary="给平台域用户分配角色",
    dependencies=[Depends(require_permission(ACCESS_UPDATE))],
)
@audit_action(action="update", resource="access", resource_id="user_id", fixed_tenant_id=PLATFORM_TENANT_ID)
async def assign_user_roles(
    user_id: Annotated[int, Path(description="用户ID")],
    request: AssignUserRolesRequest,
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """给平台域用户追加角色（幂等：已存在的关联不重复插入）。

    语义固化：平台用户只能绑平台域角色（``tenant_id == PLATFORM_TENANT_ID``），
    杜绝跨租户绑角色。
    """
    # 用户必须存在且属于平台域（缺失即 404——禁止给不存在/跨租户用户挂角色）
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == PLATFORM_TENANT_ID,
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFound("User", user_id)

    # 查已有角色，避免重复
    existing_stmt = select(UserRole.role_id).where(UserRole.user_id == user_id)
    existing = {rid for (rid,) in (await db.execute(existing_stmt)).all()}
    # 用集合判定去重，避免同一 rid 在请求里出现两次时各加一行
    fresh = [rid for rid in request.role_ids if rid not in existing]

    # 待新增角色必须存在且同属平台域
    bad: set[int] = set()
    if fresh:
        valid_ids = {
            rid
            for (rid,) in (
                await db.execute(
                    select(Role.id).where(
                        Role.id.in_(fresh),
                        Role.tenant_id == PLATFORM_TENANT_ID,
                    )
                )
            ).all()
        }
        bad = set(fresh) - valid_ids
    if bad:
        raise Conflict(
            f"仅可分配平台域（tenant_id={PLATFORM_TENANT_ID}）角色，以下 role_id 不存在或非平台域：{sorted(bad)}"
        )

    seen: set[int] = set(existing)
    added: list[int] = []
    for rid in fresh:
        if rid in seen:
            continue
        seen.add(rid)
        db.add(UserRole(user_id=user_id, role_id=rid))
        added.append(rid)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise Conflict("存在无效或已分配的角色 ID")
    # 追加角色同样 bump：角色集变化即时生效到会话快照（新登录才有新权限，
    # 旧会话凭 bump 重登取新值）
    from llmpod.services import session_version_service

    await session_version_service.bump_version(user_id, db)
    await db.commit()
    return success(RolesAssignData(user_id=user_id, added=added))


@router.post(
    "/users/{user_id}/roles/{role_id}/revoke",
    response_model=Result[UserRoleRevokeData],
    summary="回收平台域用户角色",
    dependencies=[Depends(require_permission(ACCESS_UPDATE))],
)
@audit_action(action="update", resource="access", resource_id="user_id", fixed_tenant_id=PLATFORM_TENANT_ID)
async def revoke_user_role(
    user_id: Annotated[int, Path(description="用户ID")],
    role_id: Annotated[int, Path(description="角色ID")],
    db: AsyncSession = Depends(get_db),
    user: SessionData = Depends(get_current_user),
):
    """从平台域用户回收单个角色。

    管理员角色（super_admin/tenant_admin）须保证所属域至少保留一个用户，
    若回收后该角色无用户持有则拒绝，防止平台或租户"无主"。
    """
    # 锁定角色行：串行化对该角色的并发回收，使下面的"至少保留一个管理员"
    role_stmt = (
        select(Role)
        .where(
            Role.id == role_id,
            Role.tenant_id == PLATFORM_TENANT_ID,
        )
        .with_for_update()
    )
    role = (await db.execute(role_stmt)).scalar_one_or_none()
    if role is None:
        raise NotFound("Role", role_id)

    # 目标用户是否实际持有该角色：未持有时回收为幂等 no-op，不触发保留校验，
    # 避免对"没持有该角色的用户"误报 403（原实现只统计角色总持有数）。
    held_stmt = (
        select(func.count())
        .select_from(UserRole)
        .where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
        )
    )
    held = ((await db.execute(held_stmt)).scalar() or 0) > 0

    if held and role.code in ADMIN_ROLE_CODES:
        # 统计"除目标用户外"的其他持有者；为 0 说明目标用户是最后一名管理员，
        # 回收会使该域无主，拒绝。
        others_stmt = (
            select(func.count())
            .select_from(UserRole)
            .where(
                UserRole.role_id == role_id,
                UserRole.user_id != user_id,
            )
        )
        others = (await db.execute(others_stmt)).scalar() or 0
        if others < 1:
            raise LLMPodError(f"无法回收角色 '{role.code}'：至少需保留一个管理员用户")

    await db.execute(
        delete(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
        )
    )
    await db.flush()
    # 角色集变化即时吊销该用户存量会话：access_domain/is_superuser 是登录快照，
    # 不 bump 的话降权最长要等 24h TTL 才生效（细化权限每请求回查 DB 不受影响）
    from llmpod.services import session_version_service

    await session_version_service.bump_version(user_id, db)
    await db.commit()
    return success(UserRoleRevokeData(user_id=user_id, role_id=role_id))
