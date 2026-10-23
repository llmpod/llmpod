"""NodeModel ORM 模型（模型在 Node 上的物理分发状态：期望 vs 观测）。

复合主键 (model_id, node_id)。``desired_state`` 是 manager 聚合期望（present/absent），
``status`` 是 node 回报观测（absent/downloading/verifying/present/failed）。
manager 只主动写 ``desired_state`` + 初始 ``absent``；其余 ``status`` 由 node 的
TaskEvent 驱动——这张表是 node 运行时状态的投影，不是 manager 推动的状态机。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, TimestampMixin


class NodeModel(Base, TimestampMixin):
    """模型在某 Node 上的分发状态（节点级 staging / 进度承载）。"""

    __tablename__ = "node_model"

    # 逻辑外键 → model.id / node.id（无 DB 级 FK，与全仓约定一致）
    model_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="模型ID（→ model.id）")
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="节点ID（→ node.id）")

    desired_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="absent",
        server_default="absent",
        comment="manager 聚合期望：present（需要这份）| absent（不需要）",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="absent",
        server_default="absent",
        comment="node 回报观测：absent | downloading | verifying | present | failed",
    )

    local_dir: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="下载目录（恒为目录）")
    model_path: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="引擎 --model 实际指向路径（single_gguf/gguf_shards 时是文件≠local_dir；folder 时=local_dir）",
    )

    bytes_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="模型总字节")
    bytes_done: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="已下字节（进度）",
    )

    verified_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="上次校验通过哈希")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败/不一致说明（呈现层直接展示）")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次一致性校验时间",
    )

    __table_args__ = (
        PrimaryKeyConstraint("model_id", "node_id", name="pk_node_model"),
        CheckConstraint("desired_state IN ('present', 'absent')", name="ck_node_model_desired_state"),
        CheckConstraint(
            "status IN ('absent', 'downloading', 'verifying', 'present', 'failed')",
            name="ck_node_model_status",
        ),
    )

    def __repr__(self) -> str:
        return f"<NodeModel model_id={self.model_id} node_id={self.node_id} status={self.status!r}>"
