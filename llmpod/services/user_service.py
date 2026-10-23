"""用户域服务。

当前只收口"删除/降级用户前的管理员兜底校验"这一跨路由复用的不变式
（管理端 ``/admin/users`` 与租户端 ``/tenant/users`` 的删除端点共用）。
保持与 ``routes/admin/access.py`` 的 ``revoke_user_role`` 同一语义：
不得让某管理员角色（``ADMIN_ROLE_CODES``）失去最后一名持有者，否则平台 / 租户
将"无主"而被锁死。数据隔离（租户边界）由路由层承担，本函数只做操作级判定。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.config.constants import ADMIN_ROLE_CODES
from llmpod.core.exceptions import LLMPodError
from llmpod.models.role import Role
from llmpod.models.user import User
from llmpod.models.user_role import UserRole
from llmpod.services.wallet_service import get_tenant_balance_map

log = logging.getLogger("llmpod.services.user")


async def assert_not_last_admin(db: AsyncSession, user_id: int) -> None:
    """确保删除用户不会让某管理员角色"无主"。

    若 ``user_id`` 持有 ``ADMIN_ROLE_CODES`` 中的角色，且除该用户外无人持有，
    则抛 :class:`LLMPodError`。未持管理员角色或尚有其他持有者时直接放行。
    """
    admin_role_stmt = (
        select(UserRole.role_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user_id, Role.code.in_(ADMIN_ROLE_CODES))
    )
    admin_role_ids = [rid for (rid,) in (await db.execute(admin_role_stmt)).all()]
    for rid in admin_role_ids:
        others = (
            await db.execute(
                select(func.count()).select_from(UserRole).where(UserRole.role_id == rid, UserRole.user_id != user_id)
            )
        ).scalar() or 0
        if others < 1:
            code = (await db.execute(select(Role.code).where(Role.id == rid))).scalar_one_or_none()
            raise LLMPodError(f"无法删除：用户是角色 '{code}' 的最后一名成员，至少需保留一个管理员")


# ---- 用户 → 角色 关联装配（供管理端 / 租户端用户路由批量回填 role_ids）----


async def get_role_ids_map(db: AsyncSession, user_ids: list[int]) -> dict[int, list[int]]:
    """批量取 ``user_id → [role_id, ...]`` 映射（一条 IN 查询，零 N+1）。

    未出现在结果中的 user_id 视为无角色（调用方按需补 ``[]``）。
    """
    if not user_ids:
        return {}
    stmt = select(UserRole.user_id, UserRole.role_id).where(UserRole.user_id.in_(user_ids)).order_by(UserRole.role_id)
    result = await db.execute(stmt)
    mapping: dict[int, list[int]] = {}
    for uid, rid in result.all():
        mapping.setdefault(uid, []).append(rid)
    return mapping


async def enrich_users_role_ids(db: AsyncSession, users: list[User]) -> list[User]:
    """把每个用户的 ``role_ids`` 批量回填到 ORM 实例上（in-place），返回原列表。

    供路由在返回前调用：``UserDetailOut`` 经 ``from_attributes`` 读取该实例属性。
    """
    if not users:
        return users
    mapping = await get_role_ids_map(db, [u.id for u in users])
    for u in users:
        u.role_ids = mapping.get(u.id, [])
    return users


async def enrich_users_tenant_balance(db: AsyncSession, users: list[User]) -> list[User]:
    """把每个用户所属租户的余额批量回填（in-place），返回原列表。

    余额是租户维度（同一租户的用户同值），从 wallet 批量取后按 ``tenant_id`` 回填；
    ``tenant_available = tenant_balance - tenant_frozen_balance``，与钱包路由口径一致。
    无钱包行按 0。供用户列表返回前调用。
    """
    if not users:
        return users
    tenant_ids = {u.tenant_id for u in users if u.tenant_id is not None}
    balances = await get_tenant_balance_map(db, list(tenant_ids))
    for u in users:
        balance, frozen = balances.get(u.tenant_id, (Decimal("0"), Decimal("0")))
        u.tenant_balance = balance
        u.tenant_frozen_balance = frozen
        u.tenant_available = balance - frozen
    return users


def email_unique_violation(e: IntegrityError) -> bool:
    """创建用户的 IntegrityError 是否撞在租户内邮箱唯一约束（uq_user_tenant_email）上。

    admin/tenant 两端路由的并发兜底共用（同一张 user 表、同两条唯一约束）。约束名按
    驱动两处取：asyncpg 在底层异常 ``orig.__cause__.constraint_name``（``orig`` 本身
    只有 pgcode，没有 diag）；psycopg2 在 ``orig.diag.constraint_name``。兜底路径只
    可能撞 uq_user_username / uq_user_tenant_email 两条，按名分诊文案；名字取不到
    （其它驱动/形状）由调用方回落用户名口径。
    """
    orig = getattr(e, "orig", None)
    name = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if name is None:
        name = getattr(getattr(orig, "__cause__", None), "constraint_name", None)
    return name == "uq_user_tenant_email"
