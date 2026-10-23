"""Tenant ORM 模型。"""

from __future__ import annotations

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.event import listens_for
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.config.constants import PLATFORM_TENANT_ID
from llmpod.core.exceptions import LLMPodError
from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class Tenant(Base, IntIdMixin, TimestampMixin):
    """表示多租户系统中的一个租户（组织/账户）。"""

    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="租户名称（全局唯一）")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active", comment="租户状态：active=启用 | disabled=禁用"
    )

    __table_args__ = (UniqueConstraint("name", name="uq_tenant_name"),)

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} name={self.name!r}>"


@listens_for(Tenant, "before_delete")
def _prevent_platform_tenant_delete(mapper, connection, target: Tenant) -> None:
    """ORM 层硬性拦截：禁止删除平台租户（id=PLATFORM_TENANT_ID）。"""
    if target.id == PLATFORM_TENANT_ID:
        raise LLMPodError(f"平台租户(id={PLATFORM_TENANT_ID})不可删除")
