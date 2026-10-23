"""MetricRollup ORM 模型（指标 5 分钟桶物化行）。

中央 scraper（webserver 内置）抓取各 /metrics（proxyserver 管道指标 + 节点侧
推理引擎）做 counter 差分后按 5min 桶聚合写入本表。
一行 = 一个 标签组合 × 一个 5min 桶；分位数从 latency_buckets 直方图
桶计数插值得出（桶边界与打点端 llmpod/apps/proxyserver/metrics.py 一一对应）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin


class MetricRollup(Base, IntIdMixin):
    """指标 5 分钟桶（scraper 写入；admin metrics 查询 API 读取聚合）。"""

    __tablename__ = "metric_rollup"

    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="桶起点（系统本地时区对齐，5min 网格）",
    )
    source: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="抓取目标标识（scraper target URL，多实例区分）",
    )
    # 逻辑外键 → tenant.id（空=平台直发：指标标签取值为空）
    tenant_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="租户ID（逻辑外键 → tenant.id；空=平台直发）"
    )
    api_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="API Key 行ID")
    model_alias: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="对外模型别名")
    endpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="来源端点行ID")
    deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="部署ID")
    engine: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="引擎类型")
    operation: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="操作类型")
    upstream_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="上游流向：local | external | NULL(未选路)",
    )
    status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="请求状态：success | error | interrupted | rejected",
    )
    reject_reason: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="拒绝原因")
    requests: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="桶内请求数",
    )
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="输入 token",
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="输出 token",
    )
    total_tokens: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="总 token",
    )
    cached_tokens: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="缓存命中 token",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="failover 重试次数合计",
    )
    stream_requests: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="流式请求数",
    )
    latency_sum_ms: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="延迟总和（毫秒）",
    )
    latency_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="延迟样本数",
    )
    latency_buckets: Mapped[list] = mapped_column(
        ARRAY(Integer),
        nullable=False,
        server_default=text("'{}'::int[]"),
        comment="延迟直方图桶计数（与打点端桶边界一一对应，cumulative）",
    )
    ttft_sum_ms: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        comment="TTFT 总和（毫秒）",
    )
    ttft_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="TTFT 样本数（仅流式）",
    )
    ttft_buckets: Mapped[list] = mapped_column(
        ARRAY(Integer),
        nullable=False,
        server_default=text("'{}'::int[]"),
        comment="TTFT 直方图桶计数（与打点端桶边界一一对应，cumulative）",
    )
    in_flight_peak: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="桶内在途请求峰值（gauge 采样最大值）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="写入时间",
    )

    __table_args__ = (
        Index("idx_metric_rollup_bucket", "bucket_start"),
        Index("idx_metric_rollup_deployment", "deployment_id", "bucket_start"),
        # 租户端看板/下钻按 tenant_id 收敛取窗（不扫全租户桶行）
        Index("idx_metric_rollup_tenant", "tenant_id", "bucket_start"),
    )

    def __repr__(self) -> str:
        return (
            f"<MetricRollup id={self.id} bucket={self.bucket_start} "
            f"dep={self.deployment_id!r} requests={self.requests}>"
        )
