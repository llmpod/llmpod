"""Cluster ORM 模型。"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class Cluster(Base, IntIdMixin, TimestampMixin):
    """表示一个计算集群（Docker 或 Kubernetes）。"""

    __tablename__ = "cluster"

    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="集群名称")
    # K8s 编排未实现暂不开放；恢复时本 comment 加回 "| k8s"，并放开创建接口词表里的 "k8s"
    # （存量库的列注释变更需配 revision 的 op.alter_column 带上）
    type: Mapped[str] = mapped_column(String(50), nullable=False, comment="集群类型：docker")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="集群描述（可空）")
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="active", comment="集群状态：active=启用 | inactive=停用"
    )
    spec: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", comment="集群规格（JSON 对象，如连接信息、版本等）"
    )

    __table_args__ = (
        # k8s 值为将来 K8s 编排预留（列域占位）；编排未实现前创建接口只放行 docker
        CheckConstraint("type IN ('docker', 'k8s')", name="ck_cluster_type"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_cluster_status"),
        Index("idx_cluster_type", "type"),
        Index("idx_cluster_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<Cluster id={self.id} name={self.name!r}>"
