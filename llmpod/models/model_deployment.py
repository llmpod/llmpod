"""ModelDeployment ORM 模型（CRD 风格部署实例，String PK）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, TimestampMixin


class ModelDeployment(Base, TimestampMixin):
    """模型部署（CRD 风格：核心列 + spec JSONB 扩展槽，String(64) PK 仿 Node）。"""

    __tablename__ = "model_deployment"

    deployment_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        autoincrement=False,
        comment="部署ID（UUID 字符串主键；取前 8 字符做容器名防碰撞）",
    )
    # 逻辑外键 → cluster.id / model.id（无 DB 级 FK）
    cluster_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属集群ID（→ cluster.id）")
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="部署名称")
    model_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="模型ID（→ model.id）")
    engine: Mapped[str] = mapped_column(String(50), nullable=False, comment="推理引擎：vllm | sglang | ...")
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="creating",
        comment="状态：creating | running | failed | stopped | deleting",
    )
    api_endpoint: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="对外 API 端点（部署完成后填）"
    )
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="最近一次失败原因（部署成功时清空；供 API 暴露给客户端）"
    )
    # 运行时健康（推论）：health_status/crash_count/last_crash_at 由实例 CRASHED 上报驱动
    # （deployment_service.record_deployment_crash 计数 + deployment_projection.project_upwards
    # 按副本口径投影），生命周期 status 保持 running 不动——「运行中 + 恢复中」并行可见。仅
    # status=running 期维护，creating 期为 NULL。仍是推论而非探测（实例层信号）；TODO(探测健康)：
    # 未来周期探活 api_endpoint /health 时再升级为探测，并在此追加 last_health_at 等字段。
    health_status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="运行时健康（推论）：healthy | degraded（实例异常/自动恢复中）；仅 running 期维护，creating 期为 NULL",
    )
    last_crash_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近一次实例崩溃时间（运行期 CRASHED 上报时更新）"
    )
    crash_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="运行期实例崩溃次数（自部署达成起累计；实例 CRASHED 上报 +1，不随恢复清零）",
    )
    phase: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PENDING",
        server_default="PENDING",
        comment=(
            "内部编排子态（仅 status=creating 期推进；scale 时 running 期暂驻 SCALING）："
            "PENDING | STAGING_MODEL | PULL_IMAGE | START_HEAD | WAIT_HEAD_READY | "
            "START_WORKERS | WAIT_WORKERS_READY | FINALIZING | RUNNING | SCALING | "
            "STOPPING（多 worker 阶段冻结未实现）"
        ),
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="乐观锁版本（防并发 reconcile 覆盖 phase/status）",
    )
    # desired state：扩缩意图先落库再驱动（重启后 drive_scaling 据此判定方向，不靠
    # 残留行的状态猜「这轮是扩还是缩」）。达成/回滚后清 NULL。
    scale_target: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="在途扩缩的目标份数（SCALING 期权威意图；达成/回滚后清 NULL）",
    )
    spec: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="部署 spec：topology/coordinate_mode/engine_params/health_check/...（CRD 扩展槽，GIN 索引）",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('creating', 'running', 'failed', 'stopped', 'deleting')",
            name="ck_model_deployment_status",
        ),
        CheckConstraint(
            "phase IN ('PENDING', 'STAGING_MODEL', 'PULL_IMAGE', "
            "'START_HEAD', 'WAIT_HEAD_READY', 'START_WORKERS', 'WAIT_WORKERS_READY', "
            "'FINALIZING', 'RUNNING', 'SCALING', 'STOPPING')",
            name="ck_model_deployment_phase",
        ),
        CheckConstraint(
            "health_status IS NULL OR health_status IN ('healthy', 'degraded')",
            name="ck_model_deployment_health",
        ),
        Index("idx_model_deployment_cluster_id", "cluster_id"),
        Index("idx_model_deployment_engine", "engine"),
        Index("ix_model_deployment_spec", "spec", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<ModelDeployment deployment_id={self.deployment_id[:8]} status={self.status!r}>"
