"""RolePermission ORM 模型（多对多关联表）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, PrimaryKeyConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base


class RolePermission(Base):
    """将角色与权限关联（复合主键：role_id + permission_id）。"""

    __tablename__ = "role_permission"

    # 逻辑外键 → role.id
    role_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="角色 ID（逻辑外键 → role.id）")
    # 逻辑外键 → permission.id
    permission_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="权限 ID（逻辑外键 → permission.id）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="关联创建时间",
    )

    __table_args__ = (
        PrimaryKeyConstraint("role_id", "permission_id", name="pk_role_permission"),
        Index("idx_role_permission_role_id", "role_id"),
    )

    def __repr__(self) -> str:
        return f"<RolePermission role_id={self.role_id} permission_id={self.permission_id}>"
