"""权限检查服务。

权限模型为 ``scope:resource:action`` 三元组（见 :class:`llmpod.models.permission.Permission`）。
用户可拥有多个角色（``UserRole``），其有效权限是所有角色权限的并集。数据隔离
（租户边界）由 access_domain 路由门 + handler 的 ``tenant_id`` 过滤承担，本服务只负责
"用户是否对某 ``scope:resource:action`` 持有权限"的操作级判定。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.models.permission import Permission
from llmpod.models.role import Role
from llmpod.models.role_permission import RolePermission
from llmpod.models.user_role import UserRole

log = logging.getLogger("llmpod.services.permission")


async def get_role_permissions(db: AsyncSession, role_id: int) -> list[str]:
    """获取角色的所有权限（格式：``scope:resource:action``）。"""
    stmt = (
        select(Permission.scope, Permission.resource, Permission.action)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )
    result = await db.execute(stmt)
    return [f"{s}:{r}:{a}" for s, r, a in result.all()]


async def get_user_permissions(db: AsyncSession, user_id: int) -> set[str]:
    """获取用户在所有角色上的权限并集（``{"scope:resource:action", ...}``）。"""
    stmt = (
        select(Permission.scope, Permission.resource, Permission.action)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(UserRole.user_id == user_id)
    )
    result = await db.execute(stmt)
    return {f"{s}:{r}:{a}" for s, r, a in result.all()}


async def user_has_permission(
    db: AsyncSession,
    user_id: int,
    scope: str,
    resource: str,
    action: str,
) -> bool:
    """检查用户（任一角色）是否持有 ``scope:resource:action`` 权限。

    单条 join 查询 + limit 1 取存在性（limit 不约束行数的话，同一权限被
    多个角色同时授予时 join 出多行，scalar_one_or_none 会抛
    MultipleResultsFound）；超管短路在依赖层处理，此函数不做超管判定。
    """
    stmt = (
        select(Permission.id)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(
            UserRole.user_id == user_id,
            Permission.scope == scope,
            Permission.resource == resource,
            Permission.action == action,
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


# ---- 角色 → 权限 关联装配（供角色列表 / 详情批量回填 permission_ids）----


async def get_role_permission_ids_map(db: AsyncSession, role_ids: list[int]) -> dict[int, list[int]]:
    """批量取 ``role_id → [permission_id, ...]`` 映射（一条 IN 查询，零 N+1）。

    未出现在结果中的 role_id 视为未授权（调用方按需补 ``[]``）。
    """
    if not role_ids:
        return {}
    stmt = (
        select(RolePermission.role_id, RolePermission.permission_id)
        .where(RolePermission.role_id.in_(role_ids))
        .order_by(RolePermission.permission_id)
    )
    result = await db.execute(stmt)
    mapping: dict[int, list[int]] = {}
    for rid, pid in result.all():
        mapping.setdefault(rid, []).append(pid)
    return mapping


async def enrich_roles_permission_ids(db: AsyncSession, roles: list[Role]) -> list[Role]:
    """把每个角色的 ``permission_ids`` 批量回填到 ORM 实例上（in-place），返回原列表。

    供路由在返回前调用：``RoleDetailOut`` 经 ``from_attributes`` 读取该实例属性。
    """
    if not roles:
        return roles
    mapping = await get_role_permission_ids_map(db, [r.id for r in roles])
    for r in roles:
        r.permission_ids = mapping.get(r.id, [])
    return roles
