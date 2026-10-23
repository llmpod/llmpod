"""Permission ORM 模型。"""

from __future__ import annotations

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin


class Permission(Base, IntIdMixin):
    """表示单个权限（域 + 资源 + 操作三元组）。

    权限串格式为 ``scope:resource:action``（如 ``platform:cluster:create``）。
    ``scope`` 取值 ``platform`` | ``tenant``，区分管理端 / 租户端操作权限，
    使权限选择器可按域过滤、同 resource 的平台/租户操作可独立授权/回收。
    数据隔离（租户边界）仍由 access_domain 路由门 + handler 的 tenant_id 过滤承担。
    """

    __tablename__ = "permission"

    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="权限域：platform=平台/管理端 | tenant=租户端"
    )
    resource: Mapped[str] = mapped_column(String(64), nullable=False, comment="资源标识，如 cluster / user / gpu")
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="操作标识，如 read / create / update / delete"
    )
    description: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="权限说明（可空）")

    __table_args__ = (
        UniqueConstraint("scope", "resource", "action", name="uq_permission_scope_resource_action"),
        Index("idx_permission_scope", "scope"),
        Index("idx_permission_scope_resource", "scope", "resource"),
    )

    def __repr__(self) -> str:
        return f"<Permission id={self.id} {self.scope}:{self.resource}:{self.action}>"
