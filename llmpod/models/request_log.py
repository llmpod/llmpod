"""RequestLog ORM 模型（运行态计量行）。

PG 只存计量列 + 块哈希引用（内容寻址 key），不感知 BodyStore 后端；
body 原文在块文件（{body_dir}/blocks/{hash前2位}/{hash}.gz）。
schema 归管理端 init_db.py；API 网关只写不建表。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, Numeric, String, Text, func, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.db.base import Base, IntIdMixin

# provider_name 的列宽。列定义与写入侧的截断兜底（apps/proxyserver/request_logger.py）共用
# 本常量：该列装的是「上游展示名」（端点 name，上限 255），超出列宽会被 PG 拒绝，而落账是
# fire-and-forget —— 列宽是两处共同依赖的事实，各写各的就会出现「改了一处、另一处仍截在旧宽」。
PROVIDER_NAME_MAX_CHARS = 100


class RequestLog(Base, IntIdMixin):
    """调用日志计量行（列表查询只碰这表；详情才读 BodyStore 块）。"""

    __tablename__ = "request_log"

    request_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="请求唯一ID（uuid）")
    # 逻辑外键 → tenant.id（与审计日志同一约定）。
    # NULL = 平台直发 Key 的请求（平台侧直接使用，不走租户钱包；Key 预算上限仍拦截）。
    # 平台视角 = 不传或传 0（平台域：NULL ∪ 0，与审计日志同口径）；传 N≥1 精确等值。
    tenant_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="租户ID（逻辑外键 → tenant.id；NULL=平台直发 Key 的请求；平台视角=不传或传 0 取 NULL ∪ 0，传 N≥1 精确等值）",
    )
    api_key_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="API Key 行ID（鉴权通过才落账，恒非空）")
    model_alias: Mapped[str] = mapped_column(String(255), nullable=False, comment="对外模型别名")
    served_model: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="引擎内部模型名")
    operation: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="操作：chat.completions | anthropic.messages | embeddings | rerank"
    )
    protocol: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="协议：openai | anthropic")
    engine: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="引擎类型（vllm/sglang/...）")
    upstream_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="上游流向：local(本地部署) | external(外部供应商) | NULL(未尝试转发，如无候选端点)",
    )
    provider_name: Mapped[str | None] = mapped_column(
        String(PROVIDER_NAME_MAX_CHARS),
        nullable=True,
        comment=(
            "上游展示名（快照自端点 name：本地=部署名，外部=端点名）；NULL=未触达上游。"
            "列名沿用 provider_name 是历史与前端 JSON 契约所限，语义已不是「供应商」；"
            "varchar(100) 短于端点名上限 255，写入侧按列宽截断兜底（见 apps/proxyserver/request_logger.py）"
        ),
    )
    endpoint_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="实际服务端点行ID（ModelEndpoint.id，可回溯来源）"
    )
    route_reason: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment=(
            "选路上游的原因：affinity(会话亲和命中，缓存优先) | weighted(优先级分组+组内加权随机，"
            "常规负载均衡) | failover_affinity(上游失败后换端，命中亲和) | failover_weighted"
            "(上游失败后换端，按优先级+权重重选)；NULL=历史行或未触达选路"
        ),
    )
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="输入 token")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="输出 token")
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0", comment="总 token")
    usage_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", comment="用量含断流/缺报估算值"
    )
    cached_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="缓存命中 token（prompt 子集）；NULL=引擎未上报，0=明确无命中"
    )
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True, comment="费用（usage × 定价）")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="端到端延迟（毫秒）")
    ttft_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="首帧延迟（毫秒；仅流式有值，非流式拿不到首帧时点）"
    )
    finish_reason: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="上游结束原因原样存（openai finish_reason / anthropic stop_reason；length/max_tokens = 被输出上限截断）",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="success",
        comment="success | error | interrupted（拒绝不落账：只打指标，request_log 只记真实触达上游的请求）",
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="响应 HTTP 状态码")
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", comment="failover 重试次数（首试成功=0）"
    )
    request_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="请求体块哈希（BodyStore）")
    response_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="响应体块哈希")
    conv_prefix_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="会话前缀指纹（协议感知；无会话语义为空）"
    )
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", comment="body 是否被截断（默认 32KB/字段）"
    )
    snapshot_disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", comment="快照关闭（仅记计量）"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="失败原因（status=error；粗粒度，租户可见）"
    )
    error_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="上游错误原文直存（仅管理员视图返回，租户侧 exclude；500 字符截断防超长）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, comment="请求时间"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'error', 'interrupted')",
            name="ck_request_log_status",
        ),
        Index("uq_request_log_request_id", "request_id", unique=True),
        Index("idx_request_log_api_key_id", "api_key_id"),
        Index("idx_request_log_conv_prefix", "conv_prefix_hash"),
        # 租户域查询形态（tenant_id 过滤 + 时间倒序分页/Top-N）走索引
        Index("idx_request_log_tenant_created", "tenant_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<RequestLog id={self.id} req={self.request_id[:8]} model={self.model_alias!r} status={self.status!r}>"
