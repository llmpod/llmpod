"""Route ORM 模型（对外服务：对外模型名 + 定价 + 发布态 + failover）。"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from llmpod.config.enums import ModelType
from llmpod.db.base import Base, IntIdMixin, TimestampMixin

# failover 缺省策略的唯一定义处：ORM 列默认、create_route/ensure_route 入参缺省、
# proxy 快照 wire 模型兜底全部引用这里——策略翻转只改一处
FAILOVER_ENABLED_DEFAULT = True
FAILOVER_MAX_RETRIES_DEFAULT = 2
FAILOVER_SERVER_DEFAULT = "true"


class Route(Base, IntIdMixin, TimestampMixin):
    """路由（对外服务）。

    对外模型名（client 请求的 model 字段）唯一；发布态由 status 决定，unpublished 后
    /v1/models 不再列出、调用 404。定价 per million tokens（费用 = usage/1e6 × 价格）。
    成员来源见 RouteBinding（多来源按权重组成，权重协议内归一）。
    """

    __tablename__ = "route"

    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="对外模型名（全局唯一）")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="published", comment="发布状态：published | unpublished"
    )
    origin: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="manual",
        server_default="manual",
        comment="创建来源：auto（部署自动发布，部署删除后随绑定清空而删）| manual（管理端手工建，不随部署删除）",
    )
    # 模型中心元数据：类型驱动前端分类展示（取值见 ModelType）；描述可空
    model_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=ModelType.LLM.value,
        server_default="llm",
        comment="模型类别：llm | multimodal | embedding | rerank | image | stt | tts",
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="模型描述（模型中心展示，可空）")
    # 最大上下文长度（tokens）：部署自动发布时从 topology.max_model_len 带入，管理端可手动改，可空
    context_length: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="最大上下文长度（tokens）；部署自动发布时带入 topology.max_model_len，管理端可手动改，可空",
    )
    # 能力标签（纯展示，系统不处理）：前端按需渲染徽章/筛选；如 ["思考", "工具调用", "视觉"]
    tags: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="能力标签列表（展示用，系统不处理；如 思考/工具调用/视觉）",
    )
    # 定价三列（每百万 token，元）。Route = 单模型对外别名：input 是唯一的输入 token
    # 单价——chat 的 prompt、embeddings/rerank 的全部 token 都按它计；output 仅生成类
    # 有值；cached 是 chat 缓存命中的折扣输入价。NULL = 不计费（免费）。
    input_price_per_mtok: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6),
        nullable=True,
        comment="输入 token 定价（每百万 token；chat 输入与 embeddings/rerank 全量均按此计，空=不计费）",
    )
    output_price_per_mtok: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True, comment="输出 token 定价（每百万 token，仅生成类，空=不计费）"
    )
    cached_input_price_per_mtok: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6),
        nullable=True,
        comment="缓存命中输入 token 定价（每百万；NULL=未配置，按全价计）",
    )
    failover_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=FAILOVER_ENABLED_DEFAULT,
        server_default=text(FAILOVER_SERVER_DEFAULT),
        comment="选定端点失败后是否切换到同协议其他成员（默认开；单成员路由退化行为与关闭一致）",
    )
    failover_max_retries: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=FAILOVER_MAX_RETRIES_DEFAULT,
        server_default=str(FAILOVER_MAX_RETRIES_DEFAULT),
        comment="failover 切换次数上限（默认 2，共 3 次尝试；0=不切换）",
    )

    __table_args__ = (
        CheckConstraint("status IN ('published', 'unpublished')", name="ck_route_status"),
        CheckConstraint("origin IN ('auto', 'manual')", name="ck_route_origin"),
        CheckConstraint("failover_max_retries >= 0", name="ck_route_failover_retries"),
        Index("uq_route_name", "name", unique=True),
    )

    def __repr__(self) -> str:
        return f"<Route id={self.id} name={self.name!r} status={self.status!r}>"
