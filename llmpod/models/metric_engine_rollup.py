"""MetricEngineRollup ORM 模型（引擎 gauge 5 分钟桶物化行）。

中央 scraper 抓取各节点引擎 /metrics 后，把 KV 水位 / 排队 / running / 前缀
命中率这类 gauge 按 5min 桶累计成 avg 与 max（avg 看常态、max 看峰值）；
抢占是 counter，记桶内增量。一行 = 一个引擎实例 × 一个 5min 桶，
``samples`` 是桶内成功抓取次数——区分「真的没变」与「没抓到」。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin


class MetricEngineRollup(Base, IntIdMixin):
    """引擎实例指标 5 分钟桶（scraper 写入；引擎网格历史曲线查询用）。"""

    __tablename__ = "metric_engine_rollup"

    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="桶起点（与 metric_rollup 同网格，本地时区 5min 对齐）",
    )
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="引擎实例ID")
    deployment_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="部署ID")
    engine: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="引擎类型：vllm | sglang | ...")
    kv_cache_util_avg: Mapped[float | None] = mapped_column(Float, nullable=True, comment="KV 显存水位 0-1 · 桶内均值")
    kv_cache_util_max: Mapped[float | None] = mapped_column(Float, nullable=True, comment="KV 显存水位 0-1 · 桶内峰值")
    running_requests_avg: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="并发处理中请求数 · 桶内均值"
    )
    running_requests_max: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="并发处理中请求数 · 桶内峰值"
    )
    waiting_requests_avg: Mapped[float | None] = mapped_column(Float, nullable=True, comment="排队深度 · 桶内均值")
    waiting_requests_max: Mapped[float | None] = mapped_column(Float, nullable=True, comment="排队深度 · 桶内峰值")
    prefix_cache_hit_rate_avg: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="前缀缓存命中率 0-1 · 桶内均值"
    )
    prefix_cache_hit_rate_max: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="前缀缓存命中率 0-1 · 桶内峰值"
    )
    preemptions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="桶内抢占增量（counter 差分；引擎重启按回绕口径）",
    )
    samples: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="桶内成功抓取次数（区分「没变」与「没抓到」）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="写入时间",
    )

    __table_args__ = (
        Index("idx_metric_engine_rollup_bucket", "bucket_start"),
        Index("idx_metric_engine_rollup_deployment", "deployment_id", "bucket_start"),
        Index("idx_metric_engine_rollup_instance", "instance_id", "bucket_start"),
    )

    def __repr__(self) -> str:
        return (
            f"<MetricEngineRollup id={self.id} bucket={self.bucket_start} "
            f"instance={self.instance_id[:8]} kv_avg={self.kv_cache_util_avg}>"
        )
