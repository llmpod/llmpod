"""User ORM 模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class User(Base, IntIdMixin, TimestampMixin):
    """表示租户中的一个用户账户。"""

    __tablename__ = "user"  # PG 保留字，但在此显式使用

    # 逻辑外键 → tenant.id
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属租户ID")
    username: Mapped[str] = mapped_column(String(64), nullable=False, comment="用户名（全局唯一，登录用）")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="密码哈希（不对外暴露）")
    email: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="邮箱（租户内唯一，可空）")
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="手机号（可空）")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active", comment="用户状态：active=启用 | disabled=禁用"
    )
    auth_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", comment="认证版本；权限或密码变化时同事务递增"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近一次登录时间（可空）"
    )

    __table_args__ = (
        # username 全局唯一：登录按纯 username+password 鉴别用户，无需带租户标识
        UniqueConstraint("username", name="uq_user_username"),
        UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),
        Index("idx_user_tenant_id", "tenant_id"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"
