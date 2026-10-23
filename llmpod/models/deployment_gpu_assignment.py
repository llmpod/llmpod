"""DeploymentGpuAssignment ORM 模型（GPU 分配权威来源）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin


class DeploymentGpuAssignment(Base, IntIdMixin):
    """GPU 分配记录（ResourceLedger 显存预留的持久化权威来源；rollback 同步删行）。

    无 TimestampMixin（分配时间用 allocated_at 单列即可）。
    """

    __tablename__ = "deployment_gpu_assignment"

    # 逻辑外键 → model_deployment.deployment_id / gpu.uuid / instance.instance_id（无 DB 级 FK）
    deployment_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属部署ID")
    gpu_uuid: Mapped[str] = mapped_column(String(128), nullable=False, comment="GPU UUID（→ gpu.uuid）")
    instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="所属实例ID（可空）")
    gpu_memory_utilization: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.9, server_default="0.9", comment="显存利用率上限（0-1）"
    )
    allocated_vram_gb: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0", comment="预留显存（GB）"
    )
    role: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="角色：full | prefill | ...（可空）")
    resource_type: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="gpu", comment="资源类型：gpu | cann | cpu"
    )
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="分配时间"
    )

    __table_args__ = (
        UniqueConstraint("deployment_id", "gpu_uuid", name="uq_deployment_gpu"),
        Index("idx_dga_deployment_id", "deployment_id"),
        Index("idx_dga_gpu_uuid", "gpu_uuid"),
    )

    def __repr__(self) -> str:
        return (
            f"<DeploymentGpuAssignment id={self.id} deployment_id={self.deployment_id[:8]} gpu_uuid={self.gpu_uuid!r}>"
        )
