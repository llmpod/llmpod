"""AuditLog ORM 模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin


class AuditLog(Base, IntIdMixin):
    """不可变审计日志条目，跟踪操作者在资源上的行为。"""

    __tablename__ = "audit_log"

    actor_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="操作者类型：user | system")
    # 未知/未认证主体记 0（system 预留 / 历史数据；登录失败审计只记注册用户，带真实 ID）
    actor_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="操作者ID（逻辑外键 → user.id；0=未知操作者）"
    )
    actor_name: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="操作者用户名快照")
    # 逻辑外键 → tenant.id（记录**数据归属**租户，非操作者租户）。
    # NULL = 这行没有租户维度：平台级资源（集群/节点/GPU/模型/路由/端点/系统配置等本就
    #   没有 tenant_id 列）上的操作，以及 system 主体写入的行；
    # 0 = 归属平台租户（管理员租户）自身：平台用户/角色管理、登录登出、改密等。
    # 两者不是同义：平台视角查询把 0 当「平台域」哨兵（取 NULL ∪ 0），租户视角一律精确等值。
    tenant_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="数据归属租户ID（逻辑外键 → tenant.id；空=无租户维度的平台级操作，0=平台租户自身操作）",
    )
    action: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="操作：create | update | delete | login | logout"
    )
    # 操作结果：success | failed（失败含业务异常与权限拒绝，是尝试性操作的审计线索）
    result: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="success", comment="操作结果：success | failed"
    )
    resource_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="资源类型（对齐权限 resource 串：user/tenant/api_key/...）"
    )
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="资源ID快照")
    detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", comment="明细 JSONB：params（脱敏后请求参数）/ 返回摘要 / error"
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True, comment="操作来源 IP")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="审计记录创建时间",
    )

    __table_args__ = (
        CheckConstraint("actor_type IN ('user', 'system')", name="ck_audit_log_actor_type"),
        # actor_id=0 表示未知操作者（system 预留 / 历史数据）
        Index("idx_audit_log_actor", "actor_type", "actor_id"),
        Index("idx_audit_log_tenant", "tenant_id"),
        # 租户端最高频查询形态：本租户 + 按时间倒序分页
        Index("idx_audit_log_tenant_created", "tenant_id", "created_at"),
        Index("idx_audit_log_action", "action"),
        Index("idx_audit_log_resource", "resource_type", "resource_id"),
        Index("idx_audit_log_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} actor={self.actor_type}:{self.actor_id} action={self.action!r}>"
