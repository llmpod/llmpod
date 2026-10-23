"""Wallet ORM 模型（租户钱包余额，API 网关扣费账本：余额与冻结的权威记录）。"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Index, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class Wallet(Base, IntIdMixin, TimestampMixin):
    """租户钱包（余额元，唯一钱池）。

    该租户所有 Key 的调用费用从这扣；平台 Key（tenant_id=NULL）不扣。
    钱包行在首次充值/调额时惰性创建（无行 = 余额 0 = 不足）。
    """

    __tablename__ = "wallet"

    # 逻辑外键 → tenant.id（平台租户 id=0 也可持钱包，暂不用）
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属租户ID")
    balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
        comment="总余额（元，CNY）：只被充值/调额/真实扣费/赠送改动，预扣不动",
    )
    frozen_balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
        comment="在途冻结（元）：预扣时 +、结算/解冻时 -；可用余额 = balance - frozen_balance",
    )

    __table_args__ = (Index("uq_wallet_tenant_id", "tenant_id", unique=True),)

    def __repr__(self) -> str:
        return f"<Wallet id={self.id} tenant={self.tenant_id} balance={self.balance}>"
