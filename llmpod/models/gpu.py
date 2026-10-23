"""GPU ORM 模型。"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin, TimestampMixin


class Gpu(Base, IntIdMixin, TimestampMixin):
    """表示挂载到节点上的单个 GPU 设备。"""

    __tablename__ = "gpu"

    uuid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, comment="GPU 硬件 UUID（全局唯一）")
    # 逻辑外键 → node.id
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属节点ID")
    index: Mapped[int] = mapped_column(Integer, nullable=False, comment="GPU 在节点内的序号（从 0 开始）")
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="GPU 型号名称")
    arch: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="软件栈名：nvidia / cann / cpu（驱动镜像选择/设备配置，所有 GPU 必填）"
    )
    spec: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="架构专属扩展参数槽（如 Ascend {soc_model,cann_version}）；CRD 风格，谁关注谁读",
    )
    total_vram_gb: Mapped[float] = mapped_column(Float, nullable=False, comment="显存总量（GB）")
    driver_version: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="驱动版本（可空）")
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="online",
        comment="GPU 状态：online=在线 | stale=心跳过期 | offline=离线",
    )

    __table_args__ = (
        CheckConstraint("status IN ('online', 'stale', 'offline')", name="ck_gpu_status"),
        Index("idx_gpu_node_id", "node_id"),
        Index("idx_gpu_status", "status"),
        Index("idx_gpu_arch", "arch"),
    )

    def __repr__(self) -> str:
        return f"<Gpu id={self.id} uuid={self.uuid!r}>"
