"""ModelEndpoint ORM 模型（来源端点：本地部署产出或外部登记）。

本地部署一副本一协议一条（(deployment_id, replica_id, protocol) 唯一）——多副本
部署每副本各一行进路由成员池；同一物理来源多协议建多条记录。deployment_id 可空
（NULL=外部登记端点）；api_base 是当前转发快照（本地由部署投影同步，外部写死）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class ModelEndpoint(Base, IntIdMixin, TimestampMixin):
    """来源端点（API 网关 ProxyServer 据此路由；本地部署投影 / 外部手动登记统一一张表）。"""

    __tablename__ = "model_endpoint"

    # 端点展示名列宽：deployment_projection.endpoint_display_name 按此收口超长部署名
    ENDPOINT_NAME_MAX_CHARS = 255

    # 逻辑外键 → model_deployment.deployment_id / instance.instance_id（无 DB 级 FK）
    deployment_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="所属部署ID（NULL=外部登记端点，本地=部署投影同步）"
    )
    # 逻辑外键 → model_deployment_replica.replica_id（无 DB 级 FK）
    model_deployment_replica_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="所属副本ID（→ model_deployment_replica.replica_id；一副本一协议一行，迁移期可空）",
    )
    instance_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="所属实例ID（可空，指 rank-0；四层化后由 replica 列取代，迁移期保留）"
    )
    engine_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="引擎类型：vllm | sglang | ...；外部=external"
    )
    protocol: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="openai", comment="协议：openai | anthropic（一协议一条记录）"
    )
    api_base: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="当前实际转发地址（快照；本地由部署同步，外部写死）"
    )
    path: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="协议端点子路径（不含版本根 /v1；api_base 携带版本根；本地部署由 category 派生）",
    )
    api_key: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="API Key（外部必填；本地可空）")
    served_model: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="该来源的模型名（外部=官方名 gpt-4o）"
    )
    name: Mapped[str] = mapped_column(
        String(ENDPOINT_NAME_MAX_CHARS),
        nullable=False,
        comment="端点展示名（本地=所属部署名，多副本带 ·副本N 后缀；外部=登记时自定义）。恒非空，也是「上游」列与上游依赖明细的展示值",
    )
    provider_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="供应商名（前端分组：本地集群/OpenAI/SiliconFlow…）"
    )
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上下文窗口（可空）")
    operations: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"), comment="支持的操作列表（JSON 数组）"
    )
    capabilities: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"), comment="能力标签（JSON 数组）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="手动启用开关（false 移出候选，保留记录）",
    )
    health_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="healthy",
        comment="健康状态：healthy | degraded | down",
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近一次部署投影同步时间（外部端点恒 NULL）"
    )

    __table_args__ = (
        CheckConstraint(
            "health_status IN ('healthy', 'degraded', 'down')",
            name="ck_model_endpoint_health",
        ),
        # 一协议一条端点：本地 (deployment_id, replica_id, protocol) 唯一（一副本一
        # 协议一行；replica_id 恒非空）；外部 deployment_id=NULL 允许多条（PG 唯一索引
        # 对 NULL 互不相等，天然放行）
        Index(
            "uq_model_endpoint_deployment_replica_protocol",
            "deployment_id",
            "model_deployment_replica_id",
            "protocol",
            unique=True,
        ),
        Index("idx_model_endpoint_deployment_id", "deployment_id"),
        Index("idx_model_endpoint_replica_id", "model_deployment_replica_id"),
        Index("idx_model_endpoint_health", "health_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ModelEndpoint id={self.id} name={self.name!r} protocol={self.protocol!r} health={self.health_status!r}>"
        )
