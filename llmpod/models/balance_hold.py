"""BalanceHold ORM 模型（冻结账：预扣资金的生命周期记录）。

hold 插 frozen 行，settle/release 置 settled/released；expires_at 记冻结租约到期，
hold_reaper 周期回收超期未清的悬空冻结（进程崩溃遗留），见 proxyserver/hold_reaper.py。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin


class BalanceHold(Base, IntIdMixin):
    """冻结账（在途资金生命周期）。

    request_id 唯一——同一请求只允许一条冻结记录，settle/release 按它幂等关闭。
    不落 balance_transaction：冻结不影响 balance，只在 wallet.frozen_balance 列进出。
    """

    __tablename__ = "balance_hold"

    # 逻辑外键 → tenant.id / api_key.id / route.id（无 DB 级 FK）
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属租户ID")
    api_key_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="触发 Key（逻辑外键 → api_key.id）；本表只记钱包冻结，平台 Key 无钱包冻结、不落行",
    )
    route_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="对外模型路由ID")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="关联请求ID（唯一，幂等键）")
    est_amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, comment="预扣冻结金额（元）")
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="frozen",
        comment="冻结状态：frozen | settled | released",
    )
    close_reason: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="关单原因（released 时）：upstream_error | client_abort | expired | startup",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="冻结租约到期时间：hold_reaper 据此回收超期未清的悬空冻结",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, comment="冻结发起时间"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="关单时间（settled/released）"
    )

    __table_args__ = (
        CheckConstraint("status IN ('frozen', 'settled', 'released')", name="ck_balance_hold_status"),
        Index("uq_balance_hold_request_id", "request_id", unique=True),
        Index("idx_balance_hold_status_expires", "status", "expires_at"),
        Index("idx_balance_hold_tenant_created", "tenant_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<BalanceHold id={self.id} request={self.request_id!r} status={self.status!r}>"
