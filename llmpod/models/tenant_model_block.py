"""TenantModelBlock ORM 模型（租户模型封禁）。

默认放行语义：租户默认可用全部已发布路由，管理员按需插入封禁行收窄。
仅封禁了某租户的（tenant_id, route_id）才在此出现；active=封禁中，disabled=已解除（保留记录）。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class TenantModelBlock(Base, IntIdMixin, TimestampMixin):
    """租户模型封禁（denylist）。

    平台默认对全体租户开放全部已发布路由；本表是「禁止某租户用某模型」的
    例外收窄。active 行令该租户的 Key 调用该路由 403、且模型中心/ /v1/models
    不再可见。全局封禁走 Route.status=unpublished，不落本表。
    """

    __tablename__ = "tenant_model_block"

    # 逻辑外键 → tenant.id / route.id
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="租户ID")
    route_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="路由ID（对外服务）")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active", comment="封禁状态：active | disabled"
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_tenant_model_block_status"),
        Index("uq_tenant_model_block_tenant_route", "tenant_id", "route_id", unique=True),
    )

    def __repr__(self) -> str:
        return f"<TenantModelBlock id={self.id} tenant={self.tenant_id} route={self.route_id} status={self.status!r}>"
