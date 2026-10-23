"""UserRole ORM 模型（多对多关联表）。

将用户与角色关联（复合主键：user_id + role_id），
一个用户可同时拥有多个角色。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, PrimaryKeyConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base


class UserRole(Base):
    """用户-角色关联（复合主键：user_id + role_id）。"""

    __tablename__ = "user_role"

    # 逻辑外键 → user.id
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="用户 ID（逻辑外键 → user.id）")
    # 逻辑外键 → role.id
    role_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="角色 ID（逻辑外键 → role.id）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="关联创建时间",
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "role_id", name="pk_user_role"),
        Index("idx_user_role_user_id", "user_id"),
        Index("idx_user_role_role_id", "role_id"),
    )

    def __repr__(self) -> str:
        return f"<UserRole user_id={self.user_id} role_id={self.role_id}>"
