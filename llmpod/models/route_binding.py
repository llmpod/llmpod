"""RouteBinding ORM 模型（路由 ↔ 端点的多对多成员，带 per-来源权重/优先级）。"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class RouteBinding(Base, IntIdMixin, TimestampMixin):
    """路由成员（Route ↔ ModelEndpoint 多对多带属性）。

    每个成员一个来源端点，weight 为「协议内归一权重」（openai 成员间比、anthropic 成员间比）；
    priority 大者先选（故障降级）；enabled=false 手动移除候选（保留记录）。
    """

    __tablename__ = "route_binding"

    # 逻辑外键 → route.id / model_endpoint.id（无 DB 级 FK）
    route_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属路由ID（→ route.id）")
    endpoint_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="端点ID（→ model_endpoint.id）")
    weight: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", comment="协议内归一权重（加权随机）"
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="优先级（大者先选，故障降级）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"), comment="成员开关（false 移出候选）"
    )

    __table_args__ = (
        CheckConstraint("weight >= 1", name="ck_route_binding_weight"),
        Index("uq_route_binding_route_endpoint", "route_id", "endpoint_id", unique=True),
        Index("idx_route_binding_route_id", "route_id"),
    )

    def __repr__(self) -> str:
        return f"<RouteBinding id={self.id} route={self.route_id} endpoint={self.endpoint_id} weight={self.weight}>"
