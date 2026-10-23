"""ModelDeploymentInstance ORM 模型（一个推理引擎进程组 = P实例 / D实例 / full 单体）。

引擎实例自带并行规格（tp/pp/ep/nnodes）与 GPU 架构——异构规模副本（同部署不同引擎
不同卡数）与异构架构调度（nvidia 份 + cann 份并存）都靠逐引擎自带规格表达。多机引擎
展开为多个容器分片（instance 表，node_rank 0=head）；status 是投影：全部分片 READY +
head 健康 ⇔ ready。
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, TimestampMixin


class ModelDeploymentInstance(Base, TimestampMixin):
    """引擎实例（一个副本内按模板单元各一行；full 份内一个，PD 份内 P/D 各一）。"""

    __tablename__ = "model_deployment_instance"

    instance_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        autoincrement=False,
        comment="引擎实例ID（UUID 字符串主键）",
    )
    # 逻辑外键 → model_deployment_replica.replica_id（无 DB 级 FK）
    replica_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="所属副本ID（→ model_deployment_replica.replica_id）"
    )
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="full",
        comment="引擎角色：full（单体）| prefill | decode（PD 分离槽位；prefill 永不出端点）",
    )
    tp: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", comment="本引擎张量并行度（卡数乘数一）"
    )
    pp: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", comment="本引擎流水线并行度（卡数乘数二）"
    )
    ep: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", comment="本引擎专家并行度（MoE 卡数乘数三）"
    )
    nnodes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="1",
        comment="本引擎跨机节点数（多机引擎展开为 nnodes 个容器分片，节点互异）",
    )
    gpu_arch: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="GPU 架构：nvidia | cann（调度后按调度结果填）",
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="provisioning",
        comment="投影状态：provisioning | starting | ready | down（全部分片 READY + head 健康 ⇔ ready）",
    )
    spec: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="引擎 spec 槽：target_gpu_model / target_driver_version（镜像 catalog 解析输入）等",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('provisioning', 'starting', 'ready', 'down')",
            name="ck_model_deployment_instance_status",
        ),
        CheckConstraint("role IN ('full', 'prefill', 'decode')", name="ck_model_deployment_instance_role"),
        CheckConstraint("tp >= 1", name="ck_model_deployment_instance_tp"),
        CheckConstraint("pp >= 1", name="ck_model_deployment_instance_pp"),
        CheckConstraint("ep >= 1", name="ck_model_deployment_instance_ep"),
        CheckConstraint("nnodes >= 1", name="ck_model_deployment_instance_nnodes"),
        # PD 份内 P/D 各一行；full 份内一行。role 入唯一键使约束自明
        Index("uq_model_deployment_instance_replica_role", "replica_id", "role", unique=True),
    )

    def __repr__(self) -> str:
        return f"<ModelDeploymentInstance instance_id={self.instance_id[:8]} role={self.role!r} status={self.status!r}>"
