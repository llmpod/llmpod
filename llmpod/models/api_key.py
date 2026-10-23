"""ApiKey ORM 模型（API 网关鉴权依据，管理端 CRUD 写入）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class ApiKey(Base, IntIdMixin, TimestampMixin):
    """API 网关 Key（鉴权依据；鉴权用 key_hash，明文以可逆加密列存储供管理端回显）。

    tenant_id 为空 = 平台管理端直发 Key，运行期跳过租户授权校验。
    """

    __tablename__ = "api_key"

    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="明文的 SHA-256 hex（明文不落库）")
    # 明文可逆加密落库，仅供管理端回显明文；API 网关鉴权仍用 key_hash（不可逆）
    key_plaintext_encrypted: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        default="",
        server_default="",
        comment="Key 明文的可逆加密密文（仅供管理端回显；鉴权用 key_hash）",
    )
    # 逻辑外键 → tenant.id（与审计日志/计量行同一约定）。
    # NULL = 平台直发：管理端签发恒为此形态（创建表单的「所属租户」已下线），不走租户
    # 钱包扣费；但 budget_limit 同样拦截（NULL 上限=不限，见 proxyserver/billing）。
    # 有值 = 归属该租户的 Key。
    tenant_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="所属租户ID（逻辑外键 → tenant.id；空=平台直发）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="Key 显示名")
    # 管理端写入时强制 allowed_models ⊆ 该租户可用集（published − active 封禁）（空=全部可用）
    allowed_models: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="允许调用的模型别名列表（空=全部模型可用）",
    )
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="每分钟请求数上限（空=不限）")
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="每分钟 token 上限（空=不限）")
    total_token_quota: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="Key 生命周期总 token 配额（空=不限）"
    )
    # {"别名": token 配额}；空对象 = 不启用 Key×模型档位（三档配额计数器按需启用）
    model_token_quotas: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"), comment="按模型的 token 配额映射"
    )
    # 钱包预算（软上限，钱不拆池）：从租户钱包扣钱，但按 key 累计消费做上限拦截；
    # 平台直发 Key 无钱包，但同一谓词照拦（budget_limit 空=不限）
    budget_limit: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True, comment="Key 预算上限（元，空=不限）"
    )
    budget_spent: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
        comment="Key 已真实消费（元）：只结算时累加，预扣不动",
    )
    budget_frozen: Mapped[Decimal] = mapped_column(
        Numeric(20, 6),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
        comment="Key 在途预算冻结（元）：预扣时 +、结算/解冻时 -；校验 budget_spent + budget_frozen <= budget_limit",
    )
    # 配额重置基线：reset_budget 时定格「该时刻各模型累计用量」（token）。网关
    # 启动的 rebuild_quota_counters 从 request_log 全量聚合后按模型扣基线得净增量
    # ——调用日志行不删，重置也不会被「就高回填」静默撤销。按模型分桶存：Key 总量
    # 档与 Key×模型档都要能从同一基线精确重建。
    quota_baselines: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="配额重置基线（token，按模型分桶）：reset_budget 定格累计用量，配额重建按净增量聚合",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active", comment="状态：active | disabled"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="过期时间（空=永不过期）"
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_api_key_status"),
        Index("uq_api_key_key_hash", "key_hash", unique=True),
        Index("idx_api_key_tenant_id", "tenant_id"),
    )

    @property
    def plaintext(self) -> str:
        """明文（解密 key_plaintext_encrypted 回显）；密文缺失/损坏返回空串，不抛。"""
        from llmpod.core.secrets import decrypt

        try:
            return decrypt(self.key_plaintext_encrypted or "")
        except ValueError:
            return ""

    def __repr__(self) -> str:
        return f"<ApiKey id={self.id} name={self.name!r} tenant={self.tenant_id} status={self.status!r}>"
