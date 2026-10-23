"""管理员数据看板路由（大屏数据展示）。

历史曲线读 PG（5min 桶聚合），查询逻辑在 metric_query_service；本层只做参数
校验与 Result 包装。部署实时引擎指标读 scraper 内存快照（路由在
apps/webserver/routes/admin/deployment.py）。``/screen`` 为大屏单请求聚合
（dashboard_screen_service），一屏一次往返；``/realtime`` 同理走热层
（dashboard_realtime_service），``/scrape-health`` 暴露抓取链路自监控。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.auth.deps import require_permission
from llmpod.apps.webserver.metrics_scraper import get_scraper, touch_scraper_demand
from llmpod.apps.webserver.schemas import Result, success
from llmpod.db import get_db
from llmpod.permission.permissions import DASHBOARD_READ
from llmpod.services import dashboard_anomaly_service as anomaly_svc
from llmpod.services import dashboard_overview_service as overview_svc
from llmpod.services import dashboard_realtime_service as realtime_svc
from llmpod.services import dashboard_screen_service as screen_svc
from llmpod.services import metric_query_service as svc
from llmpod.core.exceptions import LLMPodValidationError
from llmpod.services.metric_query_service import DASHBOARD_DIMS, DASHBOARD_MAX_SPAN_DAYS

router = APIRouter()

# /realtime 与 /tokens 的 dim 参数共用一个词表（service 层 DASHBOARD_DIMS 单源）
DimParam = Literal[DASHBOARD_DIMS]
# 大屏三个端点的区间参数（/realtime、/engines/history、/anomaly）。
# 区间语义与校验单源在 metric_query_service.resolve_dashboard_range：缺省今日零点
# → 当前，to 超当前钳位，反序/超跨度抛 LLMPodValidationError 转 422。本模块这三个
# 端点的聚合粒度由后端按跨度定（梯子见 dashboard_realtime_service 里带进去的界线），
# 调用方不传——注意 /model-deployments/{id}/stats 是另一个模块的端点，仍收 granularity。
_FROM_DESC = f"区间起点 ISO8601；缺省本地今日 00:00（跨度上限 {DASHBOARD_MAX_SPAN_DAYS} 天，超出 422）"
_TO_DESC = "区间终点 ISO8601；缺省当前，晚于当前按当前处理"


def _reject_removed_params(window: str | None, granularity: str | None = None) -> None:
    """旧的 ``window`` / ``granularity`` 参数显式拒掉，不静默忽略。

    两者已随区间化下线。FastAPI 对未声明的查询参数是静默丢弃的，于是没跟上的
    调用方（发 `window=1h`）会拿到 200 + 今日全量数据，KPI 环比还全是 null，
    但页面上看不出任何异常——正是「拼错参数要能从 422 里发现」要避免的形态。
    保留形参只为把这份静默变成一次可见的失败。
    """
    if window is not None or granularity is not None:
        raise LLMPodValidationError("window 和 granularity 已停用，请改用 from 和 to 指定时间范围")


class SummaryOut(BaseModel):
    """顶部摘要卡（6 个指标；时间窗由前端 from/to 决定，在线节点为实时值）。"""

    calls: int = Field(..., description="窗口内调用次数（触达上游：成功+上游错误+中断，拒绝不参与）")
    active_models: int = Field(..., description="窗口内有流量的模型数（去重）")
    active_tenants: int = Field(..., description="窗口内有调用的租户数（去重；平台 Key 不计入）")
    avg_ttft_ms: int | None = Field(None, description="平均首帧延迟（毫秒，仅流式）")
    error_rate: float = Field(..., description="错误率 0-1（错误 / 触达上游请求）")
    total_tokens: int = Field(..., description="窗口内总 token")
    online_nodes: int = Field(..., description="在线节点数（实时，不受时间窗约束）")
    total_nodes: int = Field(..., description="节点总数（实时）")


class TrendPointOut(BaseModel):
    """调用趋势单点（近 N 天逐日）。"""

    date: str = Field(..., description="日期（ISO8601，本地时区）")
    call_count: int = Field(..., description="当日调用量（触达上游）")
    error_rate: float = Field(..., description="当日错误率 0-1")


class ModelStatOut(BaseModel):
    """模型统计行。"""

    model: str | None = Field(None, description="模型名（对外别名）")
    call_count: int = Field(..., description="调用量")
    ratio: float = Field(..., description="占比 0-1（相对全模型总调用量）")
    avg_latency_ms: int | None = Field(None, description="平均端到端延迟（毫秒；无样本为 null）")
    avg_ttft_ms: int | None = Field(None, description="平均首帧延迟（毫秒；仅流式）")
    error_rate: float = Field(..., description="错误率 0-1")
    truncation_rate: float | None = Field(
        None,
        description="该模型窗口内**被输出上限截断**的占比 0-1（finish_reason=length|max_tokens；"
        "分母只数结束原因已知的请求，无此类行为 null）",
    )
    tokens: int = Field(..., description="消耗 Token 总数")


class TenantStatOut(BaseModel):
    """租户统计行。"""

    tenant_id: int = Field(..., description="租户ID")
    tenant: str = Field(..., description="租户名")
    call_count: int = Field(..., description="调用量")
    ratio: float = Field(..., description="占比 0-1（相对全租户总调用量）")


class ScreenClusterOut(BaseModel):
    """大屏·集群 GPU 池行。"""

    id: int = Field(..., description="集群ID")
    name: str = Field(..., description="集群名称")
    node_count: int = Field(..., description="集群下节点总数")
    online_node_count: int = Field(..., description="集群下在线节点数")
    gpu_count: int = Field(..., description="集群下 GPU 总数")
    gpu_utilization: float | None = Field(
        None, description="GPU 核心利用率均值 0-100（在线节点按卡数加权；无实时数据为 null）"
    )
    used_vram_gb: float | None = Field(None, description="已用显存求和（GB；无实时数据为 null）")
    total_vram_gb: float | None = Field(None, description="显存总量求和（GB；无实时数据为 null）")


class ScreenDeployCountsOut(BaseModel):
    """大屏·部署状态计数（deleting 为瞬态不展示）。"""

    running: int = Field(..., description="运行中")
    creating: int = Field(..., description="部署中")
    stopped: int = Field(..., description="已停止")
    failed: int = Field(..., description="失败")


class ScreenRecentDeploymentOut(BaseModel):
    """大屏·最近部署行。"""

    id: str = Field(..., description="部署ID")
    name: str = Field(..., description="部署名称")
    status: str = Field(..., description="部署状态：creating | running | failed | stopped | deleting")


class ScreenEventOut(BaseModel):
    """大屏·事件流行。"""

    type: str = Field(..., description="事件类型：info | success | warning | error")
    text: str = Field(..., description="事件文本（人话，可直接渲染）")
    time: str | None = Field(None, description="事件时间（格式化字符串）")


class ScreenOut(SummaryOut):
    """总览大屏整屏聚合（单请求喂饱首页）。统计卡 = Summary 全字段（同源）+ GPU/集群/部署/事件。"""

    gpu_online: int = Field(..., description="在线 GPU 数（实时）")
    gpu_total: int = Field(..., description="GPU 总数（实时）")
    clusters: list[ScreenClusterOut] = Field(..., description="集群 GPU 池")
    deployments: ScreenDeployCountsOut = Field(..., description="部署状态计数")
    recent_deployments: list[ScreenRecentDeploymentOut] = Field(..., description="最近部署（按更新时间倒序）")
    events: list[ScreenEventOut] = Field(..., description="事件流（审计+部署日志 MERGE，按时间倒序）")


def _from_or_default(from_: datetime | None) -> datetime:
    # 不给 from 不等于「全历史」：这些聚合端点按 90 天上限把缺省锚到当前往回推，
    # 与 /realtime 系（缺省今日零点）口径不同但同受跨度护栏约束
    return from_ or datetime.now(timezone.utc) - timedelta(days=DASHBOARD_MAX_SPAN_DAYS)


def _to_or_default(to: datetime | None) -> datetime:
    return to or datetime.now(timezone.utc)


@router.get("/summary", response_model=Result[SummaryOut], summary="数据看板顶部摘要卡")
async def dashboard_summary(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    from_: Annotated[datetime | None, Query(alias="from", description="起始时间 ISO8601")] = None,
    to: Annotated[datetime | None, Query(description="结束时间 ISO8601")] = None,
):
    """窗口内调用数/活跃模型/平均 TTFT/错误率/Token + 节点在线态。"""
    data = await svc.summary(
        db,
        from_=_from_or_default(from_),
        to=_to_or_default(to),
    )
    return success(SummaryOut(**data))


@router.get("/screen", response_model=Result[ScreenOut], summary="总览大屏整屏聚合（单请求）")
async def dashboard_screen(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    from_: Annotated[
        datetime | None, Query(alias="from", description="起始时间 ISO8601；缺省今日 00:00（本地）")
    ] = None,
    to: Annotated[datetime | None, Query(description="结束时间 ISO8601；缺省当前时间")] = None,
):
    """统计卡（窗口内）+ 集群 GPU 池 / 部署概览 / 事件流（实时或近窗），一次返回整屏。"""
    data = await screen_svc.screen(db, from_=from_, to=to)
    return success(ScreenOut(**data))


@router.get("/trend", response_model=Result[list[TrendPointOut]], summary="调用趋势（近 N 天）")
async def dashboard_trend(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    days: Annotated[int, Query(ge=1, le=90)] = 14,
):
    """近 N 天逐日调用量 + 错误率（空日补 0）。"""
    points = await svc.trend(db, days=days)
    return success([TrendPointOut(**p) for p in points])


@router.get("/models", response_model=Result[list[ModelStatOut]], summary="模型统计表")
async def dashboard_models(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    top_n: Annotated[int, Query(ge=1, le=50)] = 10,
    model: Annotated[str | None, Query(description="按模型别名过滤（下推 SQL）")] = None,
    tenant_id: Annotated[int | None, Query(description="按租户过滤（下推 SQL）")] = None,
):
    """按调用量排序的模型 top N（含占比/平均 TTFT/错误率/Token；可选模型/租户筛选）。"""
    items = await svc.model_ranking(
        db,
        from_=_from_or_default(from_),
        to=_to_or_default(to),
        top_n=top_n,
        model=model,
        tenant_id=tenant_id,
    )
    return success([ModelStatOut(**i) for i in items])


@router.get("/tenants", response_model=Result[list[TenantStatOut]], summary="租户统计表")
async def dashboard_tenants(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    top_n: Annotated[int, Query(ge=1, le=50)] = 10,
):
    """按调用量排序的租户 top N（含租户名；平台 Key 不计入）。"""
    items = await svc.tenant_ranking(
        db,
        from_=_from_or_default(from_),
        to=_to_or_default(to),
        top_n=top_n,
    )
    return success([TenantStatOut(**i) for i in items])


# ---- 实时大屏（/realtime）与 Token 汇总（/tokens）----


class RealtimeKpiOut(BaseModel):
    """KPI 卡（字段按卡而用：速率卡用 delta_pct，比率卡用 delta_pp，延迟卡用 delta_ms，
    计数/上下文卡用 delta）。

    ``in_flight`` 附 peak（窗口峰值）；``kv_cache_util`` 附 instance_id（最忙实例）。
    """

    value: float | None = Field(None, description="当前值（无样本时 null，前端渲染 -- 而非 0）")
    delta_pct: float | None = Field(None, description="环比对照窗 %（对照窗无数据为 null）")
    delta_pp: float | None = Field(None, description="环比百分点（比率类 0-1 小数的差 ×100）")
    delta_ms: int | None = Field(None, description="环比毫秒差（延迟类）")
    delta: float | None = Field(None, description="环比绝对差（计数/上下文类，无百分比意义时用）")
    peak: int | None = Field(None, description="窗口内峰值（在途卡）")
    instance_id: str | None = Field(None, description="最忙实例（KV 水位卡）")


class RealtimeWindowOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(..., alias="from", description="窗口起点 ISO8601")
    to: str = Field(..., description="窗口终点 ISO8601（=响应生成时刻）")
    granularity: str = Field(..., description="槽宽（如 30s / 5m）")
    source: str = Field(..., description="hot = 内存热层；cold = 5min 桶（scraper 未启动/热层无覆盖时回落）")
    age_ms: int | None = Field(None, description="热层最后一轮抓取年龄（毫秒）；冷层为 null")


class RealtimeTrendPointOut(BaseModel):
    ts: str
    qpm: float
    tpm: float
    prompt_tpm: float
    completion_tpm: float
    requests: int = Field(..., description="槽内请求计数（多日堆叠图的计数轴）")
    latency_avg_ms: int | None = Field(None, description="槽内平均延迟（毫秒；无样本为 null）")


class RealtimeTrendOut(BaseModel):
    baseline: dict[str, float] = Field(..., description="首槽绝对值（索引化基准）；键缺省 = 基准 ≤0，该序列不能索引")
    points: list[RealtimeTrendPointOut]


class RealtimeSeriesPointOut(BaseModel):
    ts: str
    value: float


class RealtimeTotalPointOut(BaseModel):
    ts: str
    tpm: float


class RealtimeCompositionItemOut(BaseModel):
    key: str = Field(..., description="维度成员；__other__ = top-5 外的长尾合并（含热层 key 上界折叠）")
    tpm: list[RealtimeSeriesPointOut]
    requests: list[RealtimeSeriesPointOut] = Field(
        ...,
        description="逐槽请求计数（与 tpm 同网格对齐；谁在涨/谁在跌由前端本地计算连续涨跌）",
    )


class RealtimeHistOut(BaseModel):
    """延迟/首帧延迟分布（cumulative 直方图桶）。"""

    buckets: list[int] = Field(..., description="13 个累计桶计数（第 i 位 = ≤ bounds[i] 的样本数，末位 = 总数）")
    bounds: list[float | None] = Field(..., description="桶上界（毫秒），末位 None = +Inf")
    avg_ms: int | None = Field(None, description="窗口平均（毫秒；无样本为 null）")
    slow_ratio: float | None = Field(None, description="超阈值样本占比（latency>2500ms / ttft>1000ms）")


class RealtimeLenHistOut(BaseModel):
    """长度分布（输入/输出 token），来自 request_log 逐请求落账。"""

    buckets: list[int] = Field(..., description="9 个累计桶计数（第 i 位 = ≤ bounds[i] 的请求数，末位 = 总数）")
    bounds: list[int | None] = Field(..., description="长度上界（token），末位 None = +Inf")
    avg_tokens: int | None = Field(None, description="平均长度（token；无样本为 null）")


class RealtimeDistributionOut(BaseModel):
    """分布面板：延迟/首字来自 rollup 直方图，长度来自 request_log 落账行。"""

    latency: RealtimeHistOut | None
    ttft: RealtimeHistOut | None
    prompt_len: RealtimeLenHistOut | None = Field(
        None, description="输入长度分布（**窗口内无落账行或全为 0 token 时为 null**）"
    )
    generation_len: RealtimeLenHistOut | None = Field(None, description="输出长度分布（同上）")


class RealtimeCompositionOut(BaseModel):
    dim: str
    total: list[RealtimeTotalPointOut]
    items: list[RealtimeCompositionItemOut]


class RealtimeEngineOut(BaseModel):
    instance_id: str
    deployment_id: str
    deployment_name: str | None = Field(None, description="部署显示名")
    engine: str | None = None
    rank: int
    status: str
    ok: bool = Field(..., description="false = 最近一轮抓取失败或从未抓到，指标字段不可信")
    metrics_expected: bool = Field(
        True,
        description="false = 实例无 API 端点（PP 多机 headless worker / 未就绪），不暴露 /metrics 属预期而非抓取故障",
    )
    kv_cache_util: float | None = None
    running_requests: float | None = None
    waiting_requests: float | None = None
    prefix_cache_hit_rate: float | None = None
    num_preemptions: float | None = None
    age_ms: int | None = Field(None, description="快照年龄（毫秒）——慢档期最长为慢档间隔，前端照实标注")


class RealtimeOut(BaseModel):
    """实时大屏整屏（KPI + 趋势 + 构成 + 分布 + 引擎网格，一次往返）。"""

    ts: str
    window: RealtimeWindowOut
    kpi: dict[str, RealtimeKpiOut] = Field(
        ...,
        description=(
            "键：qps/tpm/calls/total_tokens_kpi/avg_context/truncation_rate/output_tps/in_flight/error_rate/"
            "p95_ms/cache_hit_rate/latency_avg_ms/ttft_avg_ms/kv_cache_util/waiting_requests。"
            "除 truncation_rate 与 output_tps 来自 request_log 逐请求落账（不受 window.source 影响）外，"
            "其余均来自 metric_rollup/热层"
        ),
    )
    trend: RealtimeTrendOut
    composition: RealtimeCompositionOut
    distribution: RealtimeDistributionOut | None = Field(
        None, description="四张分布：延迟/首字来自 rollup 直方图，输入/输出长度来自 request_log 落账行"
    )
    engines: list[RealtimeEngineOut]


class TokenRowOut(BaseModel):
    key: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = Field(..., description="其中缓存：input 的子集，不得加进合计")
    total_tokens: int = Field(..., description="= input + output（不含 cached 再加）")
    ratio: float


class TokenOtherOut(TokenRowOut):
    count: int = Field(..., description="被折叠的长尾成员数")


class TokenTotalOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int


class TokensOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    dim: str
    from_: str = Field(..., alias="from")
    to: str
    rows: list[TokenRowOut]
    other: TokenOtherOut | None = None
    total: TokenTotalOut


@router.get("/realtime", response_model=Result[RealtimeOut], summary="实时大屏整屏（热层）")
async def dashboard_realtime(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    from_: Annotated[datetime | None, Query(alias="from", description=_FROM_DESC)] = None,
    to: Annotated[datetime | None, Query(description=_TO_DESC)] = None,
    prev_from: Annotated[
        datetime | None, Query(description="环比对照窗起点；与 prev_to 同给同缺、必须等长且紧邻当前区间")
    ] = None,
    prev_to: Annotated[datetime | None, Query(description="环比对照窗终点；缺省则各 delta_* 为 null")] = None,
    window: Annotated[str | None, Query(description="已下线，改用 from/to", deprecated=True)] = None,
    granularity: Annotated[str | None, Query(description="已下线，粒度由跨度决定", deprecated=True)] = None,
    dim: Annotated[
        DimParam, Query(description="构成维度：model | deployment | tenant | api_key（非法值 422）")
    ] = "model",
    model: Annotated[str | None, Query(description="按模型别名筛选整屏（空串视为未过滤）")] = None,
    tenant_id: Annotated[int | None, Query(description="按租户筛选整屏")] = None,
    api_key_id: Annotated[int | None, Query(description="按 API Key 筛选整屏")] = None,
):
    """实时整屏：跨度 ≤1h 且热层有覆盖时走 scraper 内存热层（粒度=抓取间隔），
    否则回落 5min 桶冷层；聚合粒度按跨度定（≤6h→5m、≤48h→1h、更长→1d）。

    KPI 环比对照窗由 ``prev_from`` / ``prev_to`` 给出：后端不再知道「今日」这类
    语义，无从推断该跟哪一段比。不给则各 ``delta_*`` 为 null。

    可选筛选 ``model`` / ``tenant_id`` / ``api_key_id`` 作用于所有 API 网关侧面板；
    引擎网格不受筛选影响，且带筛选时一律走冷层（``source=cold``、``in_flight``
    为 null）——语义与边界见 ``dashboard_realtime_service.RealtimeFilter``。

    一次返回 KPI + 趋势 + 构成 + 分布 + 引擎网格；构成整窗随响应返回，光标擦洗全在前端本地完成。
    """
    _reject_removed_params(window, granularity)
    # 消费即触碰：引擎抓取在需求窗口内升快档（前端无需传任何参数）
    touch_scraper_demand()
    data = await realtime_svc.realtime(
        db,
        from_=from_,
        to=to,
        prev_from=prev_from,
        prev_to=prev_to,
        dim=dim,
        model=model,
        tenant_id=tenant_id,
        api_key_id=api_key_id,
    )
    return success(RealtimeOut(**data))


@router.get("/tokens", response_model=Result[TokensOut], summary="Token 汇总表（维度切换）")
async def dashboard_tokens(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    dim: Annotated[DimParam, Query(description="汇总维度：model | deployment | tenant | api_key")] = "model",
    from_: Annotated[datetime | None, Query(alias="from", description="缺省今日 00:00（本地）")] = None,
    to: Annotated[datetime | None, Query(description="缺省当前时间")] = None,
    top: Annotated[int, Query(ge=1, le=50)] = 5,
    model: Annotated[str | None, Query(description="按模型别名过滤（维度列之外的列过滤）")] = None,
    tenant_id: Annotated[int | None, Query(description="按租户过滤（维度列之外的列过滤）")] = None,
):
    """按维度聚合的 Token 汇总（input/output/其中缓存/合计/占比 + 长尾折叠 + 合计行）。"""
    data = await svc.token_summary(db, dim=dim, from_=from_, to=to, top=top, model=model, tenant_id=tenant_id)
    return success(TokensOut(dim=dim, **data))


@router.get("/scrape-health", response_model=Result[dict[str, Any]], summary="指标抓取链路自检")
async def dashboard_scrape_health(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
):
    """抓取链路自身的健康（每个 target 最近成功/连续失败、待落桶积压、档位现状）。

    监控链路坏掉时大屏只会安静显示旧值或空值——这个端点是判断「数据源是否
    还活着」的入口。scraper 未启动返回 enabled=False。
    """
    scraper = get_scraper()
    if scraper is None:
        return success({"enabled": False, "reason": "metrics scraper not running"})
    return success({"enabled": True, **scraper.health()})


# ---- 平台总览屏（屏 1）/ 引擎历史与异常（屏 3）----


class OverviewKpiCardOut(BaseModel):
    """单张 KPI 卡（delta 量纲按卡而用；昨日无数据时 delta 全 null）。"""

    value: float | int | None = Field(None, description="当前值（无样本为 null）")
    delta_pct: float | None = Field(None, description="昨日同期 %")
    delta: float | int | None = Field(None, description="绝对数差（计数类）")
    delta_pp: float | None = Field(None, description="百分点差（比率类）")
    delta_ms: int | None = Field(None, description="毫秒差（延迟类）")
    week_total: int | None = Field(None, description="本周累计（新增租户卡）")


class OverviewTrendPointOut(BaseModel):
    hour: int = Field(..., description="窗内小时偏移（0 = 今日零点）")
    calls: int = Field(..., description="今日该小时调用量（触达上游）")
    calls_yesterday: int = Field(..., description="昨日同一小时调用量（未走到的时段为 0）")


class OverviewTrendOut(BaseModel):
    points: list[OverviewTrendPointOut]
    today_total: int
    yesterday_same_period_total: int


class OverviewUpstreamCardOut(BaseModel):
    share: float = Field(..., description="占今日上游请求总量 0-1")
    success_rate: float | None = Field(None, description="成功率 0-1（无触达请求为 null）")


class OverviewUpstreamProviderOut(BaseModel):
    name: str
    share: float
    success_rate: float | None


class OverviewUpstreamOut(BaseModel):
    local: OverviewUpstreamCardOut
    external: OverviewUpstreamCardOut
    providers: list[OverviewUpstreamProviderOut]


class OverviewServiceOut(BaseModel):
    """服务状态实时块（与指标时间窗无关）。"""

    online_models: int = Field(..., description="RUNNING 部署覆盖的模型数（去重）")
    replicas: int = Field(..., description="活跃实例数（RUNNING/READY/STARTING）")
    engine_mix: dict[str, int] = Field(..., description="引擎 → 活跃实例数")
    deployments: ScreenDeployCountsOut
    recent_deployments: list[ScreenRecentDeploymentOut]
    nodes: dict[str, int] = Field(..., description="{online, total}")
    gpu: dict[str, int] = Field(..., description="{online, total}")
    clusters: list[ScreenClusterOut]


class OverviewOut(BaseModel):
    """平台总览整屏（今日 vs 昨日同期 + 趋势双线 + 上游构成 + 服务状态，一次往返）。"""

    ts: str
    window: dict[str, str] = Field(..., description="{from: 今日零点, to: 现在}")
    kpi: dict[str, OverviewKpiCardOut] = Field(
        ...,
        description=("calls/active_tenants/active_models/new_tenants/success_rate/avg_ttft_ms（delta 对照昨日同期）"),
    )
    trend: OverviewTrendOut = Field(..., description="小时双线（今日 vs 昨日同期）")
    upstream: OverviewUpstreamOut
    service: OverviewServiceOut


@router.get("/overview", response_model=Result[OverviewOut], summary="平台总览整屏（今日 vs 昨日同期）")
async def dashboard_overview(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
):
    """屏 1 整屏：双窗 KPI（对照=昨日同时刻）+ 小时双线趋势 + 上游构成 + 服务状态实时块。"""
    data = await overview_svc.overview(db)
    return success(OverviewOut(**data))


class EngineHistoryPointOut(BaseModel):
    ts: str = Field(..., description="桶起点 ISO8601")
    kv_avg: float | None = Field(None, description="KV 水位格内均值 0-1（按抓取次数加权；无样本 null）")
    kv_max: float | None = Field(None, description="KV 水位格内峰值 0-1")
    preemptions: int = Field(..., description="格内抢占增量")
    samples: int = Field(..., description="格内成功抓取次数之和（0=没抓到，非水位为 0；亦是 kv_avg 的权重）")


class EngineHistoryInstanceOut(BaseModel):
    instance_id: str
    deployment_id: str
    deployment_name: str | None
    engine: str | None
    rank: int
    status: str
    series: list[EngineHistoryPointOut] = Field(
        ..., description="等宽桶序列（宽度见 window.granularity）；缺行=空档，不伪造 0"
    )


class EngineHistoryOut(BaseModel):
    window: dict[str, str] = Field(..., description="{from, to, granularity}")
    instances: list[EngineHistoryInstanceOut]


@router.get(
    "/engines/history",
    response_model=Result[EngineHistoryOut],
    summary="引擎实例 KV 历史（热力图）",
)
async def dashboard_engine_history(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    from_: Annotated[datetime | None, Query(alias="from", description=_FROM_DESC)] = None,
    to: Annotated[datetime | None, Query(description=_TO_DESC)] = None,
    window: Annotated[str | None, Query(description="已下线，改用 from/to", deprecated=True)] = None,
):
    """活跃实例 × 等宽桶的 KV 水位 / 抢占 / 抓到次数序列（llama.cpp 无 /metrics 渲染 --）。

    单元格宽度随跨度走（≤6h 为 5min、≤48h 为 1h、更长按日合并），取值见响应
    ``window.granularity``。
    """
    _reject_removed_params(window)
    data = await realtime_svc.engine_history(db, from_=from_, to=to)
    return success(EngineHistoryOut(**data))


class AnomalyItemOut(BaseModel):
    kind: str = Field(..., description="类别：拒绝原因（rate_limited/...）或错误粗分类（upstream_timeout/...）")
    count: int
    share: float = Field(..., description="占异常总量（错误+拒绝）的 0-1")
    side: str = Field(..., description="rejected = 准入拒绝（rollup）；error = 上游错误（request_log）")


class AnomalyBlockOut(BaseModel):
    total: int = Field(..., description="错误 + 拒绝计数合计")
    ratio: float = Field(..., description="占窗口总请求（rollup 同窗）的 0-1")
    items: list[AnomalyItemOut]


class AnomalyUpstreamCardOut(BaseModel):
    success_rate: float | None
    latency_avg_ms: int | None
    qpm: float = Field(..., description="窗口内每分钟触达请求数")


class AnomalyUpstreamProviderOut(BaseModel):
    name: str
    success_rate: float | None
    latency_avg_ms: int | None
    qpm: float


class AnomalyUpstreamOut(BaseModel):
    local: AnomalyUpstreamCardOut | None
    external: AnomalyUpstreamCardOut | None
    providers: list[AnomalyUpstreamProviderOut]


class AnomalyOut(BaseModel):
    ts: str
    window: dict[str, str]
    anomaly: AnomalyBlockOut
    upstream: AnomalyUpstreamOut


@router.get("/anomaly", response_model=Result[AnomalyOut], summary="异常信息 + 上游健康")
async def dashboard_anomaly(
    _perm: None = Depends(require_permission(DASHBOARD_READ)),
    db: AsyncSession = Depends(get_db),
    from_: Annotated[datetime | None, Query(alias="from", description=_FROM_DESC)] = None,
    to: Annotated[datetime | None, Query(description=_TO_DESC)] = None,
    window: Annotated[str | None, Query(description="已下线，改用 from/to", deprecated=True)] = None,
):
    """异常面板：准入拒绝归因（rollup）+ 错误粗分类（request_log）+ 上游健康卡。"""
    _reject_removed_params(window)
    data = await anomaly_svc.anomaly(db, from_=from_, to=to)
    return success(AnomalyOut(**data))
