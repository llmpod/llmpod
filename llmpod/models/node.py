"""Node ORM 模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, TimestampMixin


class Node(Base, TimestampMixin):
    """表示集群中的一个物理或虚拟节点。

    主键是由 Manager 分配的 VARCHAR(64) UUID，非自增。
    """

    __tablename__ = "node"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, autoincrement=False, comment="节点ID（由 Manager 分配的 UUID 字符串主键）"
    )
    # 逻辑外键 → cluster.id
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属集群ID")
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, comment="节点主机名")
    ip: Mapped[str] = mapped_column(String(45), nullable=False, comment="节点 IP 地址")
    net_ifname: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="持有 ip 的物理网卡名（多机分布式通信 SOCKET_IFNAME 注入用；空=节点未上报）",
    )
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="备注（可空）")
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="offline",
        comment="节点状态：online | offline | maintenance | busy | drained",
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近一次心跳时间（可空）"
    )
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="CPU 核数（可空）")
    cpu_arch: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="CPU 架构，如 x86_64 / arm64（可空）"
    )
    memory_gb: Mapped[float | None] = mapped_column(Float, nullable=True, comment="内存总量（GB，可空）")
    capabilities: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]", comment="节点上报的能力标签列表（JSON 数组）"
    )
    allowed_capabilities: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]", comment="允许该节点使用的能力标签列表（JSON 数组）"
    )
    version: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="节点运行的 llmpod 版本（$hello/$heartbeat 上报；比对 manager 版本得 version_status）",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('online', 'offline', 'maintenance', 'busy', 'drained')",
            name="ck_node_status",
        ),
        Index("idx_node_cluster_id", "cluster_id"),
        Index("idx_node_status", "status"),
        Index("idx_node_last_heartbeat", "last_heartbeat_at"),
    )

    def __repr__(self) -> str:
        return f"<Node id={self.id} hostname={self.hostname!r}>"
