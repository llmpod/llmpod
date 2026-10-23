"""Role ORM 模型。"""

from __future__ import annotations

from sqlalchemy import Boolean, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class Role(Base, IntIdMixin, TimestampMixin):
    """表示租户中用于 RBAC 的角色。"""

    __tablename__ = "role"

    # 逻辑外键 → tenant.id
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属租户ID")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="角色名称（展示用）")
    code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="角色编码（用于区分内置角色、超管等）")
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", comment="是否内置角色（内置角色不可删除）"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_role_tenant_code"),
        Index("idx_role_tenant_id", "tenant_id"),
    )

    def __repr__(self) -> str:
        return f"<Role id={self.id} code={self.code!r}>"
