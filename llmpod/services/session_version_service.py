"""数据库权威的用户会话版本；变更和角色/密码更新在同一事务提交。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.models.user import User


async def bump_version(user_id: int, db: AsyncSession) -> None:
    """吊销该用户所有旧会话；调用方负责提交当前事务。"""
    # 锁等待结束后必须刷新 identity map 中的旧对象，否则并发吊销可能把版本写回旧值。
    row = (
        await db.execute(
            select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is not None:
        row.auth_version = (row.auth_version or 1) + 1
