"""ModelDeploymentReplica ORM 模型（部署模板的一份完整拷贝）。

副本 = 隔离边界 / 故障域 / 伸缩单位三合一：份内配对与通信自成一体（P_i 只喂 D_i），
跨副本零交互；任一引擎实例死 ⇒ 整份判死。status 是投影不是权威来源——由份内引擎实例
状态重算（份内全部引擎 ready ⇔ serving；任一 down ⇔ down），写点在投影链。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, TimestampMixin


class ModelDeploymentReplica(Base, TimestampMixin):
    """部署副本（一个 ModelDeployment 按模板展开为 N 份，seq 0..N-1）。"""

    __tablename__ = "model_deployment_replica"

    replica_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        autoincrement=False,
        comment="副本ID（UUID 字符串主键）",
    )
    # 逻辑外键 → model_deployment.deployment_id（无 DB 级 FK）
    deployment_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="所属部署ID（→ model_deployment.deployment_id）"
    )
    seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="份内序号（部署内 0..copies-1；缩容 LIFO 从最大 seq 删）",
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="provisioning",
        comment="投影状态：provisioning | serving | down（份内全部引擎 ready ⇔ serving；任一死 ⇔ down）",
    )
    spec: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="副本 spec 槽（未来份级参数：整份重启计数等）",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('provisioning', 'serving', 'down')",
            name="ck_model_deployment_replica_status",
        ),
        CheckConstraint("seq >= 0", name="ck_model_deployment_replica_seq"),
        Index("uq_model_deployment_replica_deployment_seq", "deployment_id", "seq", unique=True),
    )

    def __repr__(self) -> str:
        return f"<ModelDeploymentReplica replica_id={self.replica_id[:8]} seq={self.seq} status={self.status!r}>"
