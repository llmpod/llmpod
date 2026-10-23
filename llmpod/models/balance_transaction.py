"""BalanceTransaction ORM 模型（钱包流水账，审计行）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin


class BalanceTransaction(Base, IntIdMixin):
    """钱包流水（审计账）。

    每笔带 balance_after（该笔后余额）可对账；FK-less + 名称快照，删主体不断账。
    hold（预扣）不落流水，只记最终资金变动：consume（结算扣费）/ topup（充值）/
    adjust（管理调额）/ system（系统赠送）。下游失败的解冻不再写流水（见 balance_hold）。
    """

    __tablename__ = "balance_transaction"

    # 逻辑外键 → tenant.id（谁付钱）
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属租户ID")
    api_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="触发 Key（充值/调额为空）")
    api_key_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="Key 显示名快照")
    route_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="对外模型路由ID")
    route_name: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="对外模型名快照")
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="关联 request_log 请求ID")
    type: Mapped[str] = mapped_column(String(20), nullable=False, comment="流水类型：consume|topup|adjust|system")
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, comment="变动金额（元，正入负出）")
    balance_after: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False, comment="该笔后余额（元）")
    meta: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="附加：tokens/operation/protocol/served_model",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, comment="流水时间"
    )

    __table_args__ = (
        CheckConstraint("type IN ('consume', 'topup', 'adjust', 'system')", name="ck_balance_txn_type"),
        Index("idx_balance_txn_tenant", "tenant_id", "created_at"),
        Index("idx_balance_txn_request_id", "request_id"),
    )

    def __repr__(self) -> str:
        return f"<BalanceTransaction id={self.id} tenant={self.tenant_id} type={self.type!r} amount={self.amount}>"
