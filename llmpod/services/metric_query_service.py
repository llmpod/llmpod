"""指标查询 service（metric_rollup 5min 桶 → 数据看板聚合）。

dashboard 路由的数据层：聚合尽量下推 SQL（GROUP BY + SUM；Token 汇总不逐行
拉桶行——整窗原始行含没人读的直方图数组列，组合数 × 桶数规模下会产生
数百 MB 瞬时分配，而这个查询发生在管理端进程里），分位数在 Python 侧合并
latency_buckets 直方图后插值。TTFT 均值（仅流式）直接取 ttft_sum/count。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.proxyserver.metrics import _LATENCY_BUCKETS
from llmpod.config.constants import NodeStatus
from llmpod.core.exceptions import LLMPodValidationError
from llmpod.models import MetricRollup, Node, RequestLog, Tenant
from llmpod.utils.time import now_local

# 桶边界唯一定义处 = proxyserver 打点端 _LATENCY_BUCKETS（12 边界 + inf）；
# 分位数插值/桶合并都按它对位，新增边界只改打点端一处
LATENCY_BUCKET_BOUNDS = _LATENCY_BUCKETS

# 上游表示「生成撞到输出上限」的结束原因：openai 用 length、anthropic 用 max_tokens。
# ``request_log.finish_reason`` 原样存上游取值，翻译只在本处一处做——截断率
# （/realtime 的 KPI 与 /models 的列）都从这里取口径。
TRUNCATED_FINISH_REASONS = ("length", "max_tokens")

GRANULARITY_MINUTES = {"5m": 5, "1h": 60, "1d": 1440}

# 维度名 → MetricRollup 列（部署下钻用）
_DIMENSION_COLUMNS = {
    "deployment": MetricRollup.deployment_id,
    "endpoint": MetricRollup.endpoint_id,
    "api_key": MetricRollup.api_key_id,
    "model": MetricRollup.model_alias,
    "engine": MetricRollup.engine,
    "tenant": MetricRollup.tenant_id,
}

# 大屏构成（/realtime）与 Token 汇总（/tokens）共用的维度子集。路由参数的
# Literal 以此为准（有单测对账）——词表散落多处时新增维度会漏改不报错
DASHBOARD_DIMS = ("model", "deployment", "tenant", "api_key")

# 大屏区间上限（天）。与 /rankings 的 days 上限同值：超出的请求一律 422，
# 不静默截断——截断会让调用方拿到一份「看起来正常但少了几天」的图。
DASHBOARD_MAX_SPAN_DAYS = 90


def resolve_dashboard_range(
    from_: datetime | None,
    to: datetime | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """大屏区间归一：(from, to) 缺省今日零点 → 当前；to 超当前钳到当前。

    缺省语义与 ``resolve_day_window`` 一致（前端不传区间时就是「今日」），
    ``_as_local`` 保证 naive 时间戳按本地时区解释——否则与 ``now_local()``
    这类 aware 值比较会直接 TypeError。

    非法区间抛 ``LLMPodValidationError``（webserver 统一映射 422）：反序、零跨度、
    超过 ``DASHBOARD_MAX_SPAN_DAYS``。抛领域异常而不是 ValueError——后者没有
    处理器接，会以 500 冒出去。
    """
    # 先钳位再推缺省起点：反过来的话「只给 to 且 to 落在下一自然日」会推出一个
    # 比钳位后的 to 还晚的起点，把一个合法请求（前端 23:59 按本地钟算出次日零点）
    # 判成反序 422
    to = _as_local(to) or now
    if to > now:
        # 前端按本地钟算 to，与服务端有秒级偏差是常态；钳位而不是报错
        to = now
    from_ = _as_local(from_) or to.replace(hour=0, minute=0, second=0, microsecond=0)
    if from_ >= to:
        raise LLMPodValidationError(f"区间非法：起点 {from_.isoformat()} 必须早于终点 {to.isoformat()}")
    # 用总秒数而不是 .days：timedelta.days 向下取整，90d23h 的 .days 也是 90，
    # 会放行到近 91 天——上限是当前唯一的成本防线，不能有这段余量
    if (to - from_).total_seconds() > DASHBOARD_MAX_SPAN_DAYS * 86400:
        raise LLMPodValidationError(f"区间非法：跨度不得超过 {DASHBOARD_MAX_SPAN_DAYS} 天")
    return from_, to


# 对照窗必须紧邻当前区间：间隔上限一天，正好覆盖「今日 vs 昨日同期」这一最松的预设。
# 这个上界是成本约束不是产品规则——两个窗是当成**一段连续区间**扫的（见
# realtime 里的 load_agg_rows），对照窗随便往回放等于让这一段无限长。
_COMPARE_MAX_GAP_SECONDS = 86400


def resolve_compare_range(
    prev_from: datetime | None,
    prev_to: datetime | None,
    from_: datetime,
    to: datetime,
    to_shift_seconds: float = 0.0,
) -> tuple[datetime | None, datetime | None]:
    """环比对照窗校验：同给同缺、与当前窗**等长**、且紧邻当前区间。

    对照窗由调用方给出（后端不知道「今日」这类语义），但它会直接当 SQL 左界用，
    所以不能原样透传：

    - 不给 → 什么都不比（各 delta 置 null）；只给一半是调用方的错；
    - **等长**是硬要求：计数型 delta（``calls`` / ``total_tokens``）不做跨度折算，
      拿 24h 对照 1h 会报出 24 倍级的偏差，而响应里看不出任何异常。

    ``to_shift_seconds`` = 服务端把当前窗右端钳回自己 ``now`` 时后退的秒数（客户端
    钟快于服务端、且快出的量超过 1 秒）。钳位量按**同量从 ``prev_to`` 扣掉**：两个窗
    一起缩短，等长不变量就不受钳位影响。只扣右端不扣左端——今日窗的「昨日同期」锚在
    昨日零点上，左端被推动就不再是同期。少了这一步，钟差就会被报成「对照窗与当前区间
    必须等长」，而这个错跟调用方给的参数其实毫无关系。
    """
    if prev_from is None and prev_to is None:
        return None, None
    if prev_from is None or prev_to is None:
        raise LLMPodValidationError("区间非法：prev_from 与 prev_to 必须同时给出")
    prev_from = _as_local(prev_from)
    prev_to = _as_local(prev_to)
    if to_shift_seconds:
        prev_to = prev_to - timedelta(seconds=to_shift_seconds)
    if prev_from >= prev_to:
        raise LLMPodValidationError(f"区间非法：对照窗起点 {prev_from.isoformat()} 必须早于终点 {prev_to.isoformat()}")
    if prev_to > to:
        raise LLMPodValidationError("区间非法：对照窗终点不得晚于当前区间终点")
    if abs((prev_to - prev_from).total_seconds() - (to - from_).total_seconds()) > 1:
        raise LLMPodValidationError("区间非法：对照窗与当前区间必须等长（计数型 delta 不做跨度折算）")
    if (from_ - prev_to).total_seconds() > _COMPARE_MAX_GAP_SECONDS:
        raise LLMPodValidationError(f"区间非法：对照窗与当前区间之间不得超过 {_COMPARE_MAX_GAP_SECONDS // 3600} 小时")
    return prev_from, prev_to


def bucket_floor_expr(col, width_seconds: int, tz_off: int):
    """列 → 本地对齐的桶起点序号表达式（epoch 取整）。

    ``tz_off`` 是本地时区偏移秒数。5m/1h 这类桶在整小时时区下不加偏移也恰好
    落在本地边界（28800 是 300 与 3600 的整数倍），但 **86400 不是**——日桶不加
    偏移会切在 UTC 零点，也就是本地早上八点。偏移量本身在任何真实时区下都是
    900 的整数倍，因此加上它仍然整除 5min 落库网格。
    """
    return func.floor((func.extract("epoch", col) + tz_off) / width_seconds)


# 触达上游的终态集合（拒绝 excluded：准入阶段被打回，未触达上游，不参与任何口径）。
# 跨屏契约：/summary /overview /realtime 的「触达」口径都以此为准（overview/realtime import）
FORWARDED_STATUSES = ("success", "error", "interrupted")


# 上游聚合行 → 成功率（分母 = 触达上游的请求，拒绝不算错误也不进分母）。
# overview 的 upstream 卡与 anomaly 的上游健康卡共用同一算式，两侧卡片数值必须对得上
def truncation_rate(truncated: int, known: int) -> float | None:
    """被输出上限截断的比例（分母 = 结束原因已知的请求数）。

    /realtime 的 KPI 与 /models 的列共用本算式——两处对同一窗口同一模型必须给
    同一个数（与 upstream_success_rate 同款：算式只写一处）。无已知样本返回 None。
    """
    return round(truncated / known, 4) if known > 0 else None


def upstream_success_rate(requests: int, errors: int, rejected: int) -> float | None:
    forwarded = requests - rejected
    if forwarded <= 0:
        return None
    return round((forwarded - errors) / forwarded, 4)


async def load_rows(
    db: AsyncSession,
    from_: datetime,
    to: datetime,
    tenant_id: int | None = None,
) -> list[MetricRollup]:
    """窗口桶行加载的公开入口（调用方已保证 from/to 有效；租户收敛可选）。

    同包 service（两端大屏共用一次取行）经此入口复用；模块内部等价于 _load_rows。
    """
    return await _load_rows(db, from_, to, tenant_id=tenant_id)


def resolve_day_window(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime]:
    """两端大屏共用的默认窗口：缺省今日 00:00（本地）→ 当前。

    入参先经 _as_local 归一（naive 视为本地时区），否则客户端传无时区时间戳
    会在调用方的 min/max 里与 now_local() 的 aware 值直接比较而抛 TypeError。
    """
    to = _as_local(to) or now_local()
    from_ = _as_local(from_) or to.replace(hour=0, minute=0, second=0, microsecond=0)
    return from_, to


def _local_tz():
    return now_local().tzinfo


# 部署详情历史曲线的期望：≤7 天仍按小时给点。下钻排查看的是「哪个小时开始的」，
# 超过一周才退到日粒度。大屏要的是另一套（跨日就看日，48h），那个常量归
# dashboard_realtime_service 自己声明——两边各说各的期望，不共用一个数。
_DEPLOYMENT_HOUR_MAX_SPAN_SECONDS = 7 * 86400


def local_offset_seconds() -> int:
    """**服务端**本地时区偏移秒数（桶按服务端本地边界对齐时的偏移量）。

    取服务端时区而不是请求时间戳自带的偏移：调用方可能用 ``toISOString()``
    这类 UTC 编码（``…Z``）传时间戳，``utcoffset()`` 会是 0，日桶就切回 UTC
    零点——同一个逻辑区间换一种编码得到不同的日界。桶网格本来就写在服务端
    本地时区上（``metrics_scraper._bucket_floor``），偏移量必须与它同源。
    """
    return int(now_local().utcoffset().total_seconds())


# 5m/1h 的分界：6 小时以内看分钟级是共识，全局固定
_SPAN_5M_LIMIT_SECONDS = 6 * 3600


def pick_granularity(
    from_: datetime,
    to: datetime,
    *,
    hourly_span_limit_seconds: float,
) -> str:
    """跨度 → 粒度：≤6h→5min；≤``hourly_span_limit_seconds``→1h；更长→1d。

    小时/日 的分界**由调用方给出**：多长的跨度还该按小时看，取决于那张图画来干嘛
    ——大屏要「跨日就看日」，部署详情页那条下钻曲线 3 天给 72 个小时点才有意义。
    函数不替调用方判断「你是谁、你该要什么」，也不为此维护两套梯子：期望由调用方
    显式带进来，这里只做一次算术。
    """
    span = (to - from_).total_seconds()
    if span <= _SPAN_5M_LIMIT_SECONDS:
        return "5m"
    if span <= hourly_span_limit_seconds:
        return "1h"
    return "1d"


def bucket_seconds_for(from_: datetime, to: datetime, *, hourly_span_limit_seconds: float) -> int:
    """区间 → 聚合桶宽（秒）；与 ``pick_granularity`` 同一套算术。"""
    return GRANULARITY_MINUTES[pick_granularity(from_, to, hourly_span_limit_seconds=hourly_span_limit_seconds)] * 60


def percentile_from_buckets(buckets: list[int], bounds: tuple[int, ...], q: float) -> int | None:
    """直方图桶计数（cumulative）→ 分位数插值。

    buckets 与 bounds 顺序对应且 buckets[len(bounds)] 为 +Inf 桶（总样本数）。
    在覆盖目标分位的桶内做线性插值；空样本返回 None。
    """
    if not buckets or len(buckets) < len(bounds) + 1:
        return None
    total = buckets[-1]
    if total <= 0:
        return None
    target = total * q
    prev_count = 0
    prev_bound = 0
    for bound, count in zip(bounds, buckets):
        if count >= target:
            # 目标落在 (prev_bound, bound] 桶内，线性插值
            span = bound - prev_bound
            in_bucket = target - prev_count
            width = count - prev_count
            if width <= 0:
                return bound
            return int(prev_bound + span * in_bucket / width)
        prev_count, prev_bound = count, bound
    # 目标分位落在 +Inf 桶（所有样本都超过最后一个有限边界）：返回最后边界，
    # 并保留「低分位也被钳制」的可辨识性——avg（latency_sum/count）仍是真的，
    # p50=p95=p99=60000 即「全部请求超最大桶」的信号
    return bounds[-1]


def merge_bucket_rows(rows: list[MetricRollup]) -> dict:
    """多行（多 source / 多标签组合）的桶计数/延迟统计合并为单视图。"""
    merged = {
        "requests": 0,
        "errors": 0,
        "interrupted": 0,
        "rejected": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "stream_requests": 0,
        "retry_count": 0,
        "latency_sum_ms": 0,
        "latency_count": 0,
        "latency_buckets": [0] * (len(LATENCY_BUCKET_BOUNDS) + 1),
        "ttft_sum_ms": 0,
        "ttft_count": 0,
        "ttft_buckets": [0] * (len(LATENCY_BUCKET_BOUNDS) + 1),
        "in_flight_peak": 0,
    }
    for r in rows:
        merged["requests"] += r.requests or 0
        if r.status == "error":
            merged["errors"] += r.requests or 0
        elif r.status == "interrupted":
            merged["interrupted"] += r.requests or 0
        elif r.status == "rejected":
            merged["rejected"] += r.requests or 0
        merged["prompt_tokens"] += r.prompt_tokens or 0
        merged["completion_tokens"] += r.completion_tokens or 0
        merged["total_tokens"] += r.total_tokens or 0
        merged["cached_tokens"] += r.cached_tokens or 0
        merged["stream_requests"] += r.stream_requests or 0
        merged["retry_count"] += r.retry_count or 0
        merged["latency_sum_ms"] += r.latency_sum_ms or 0
        merged["latency_count"] += r.latency_count or 0
        _merge_buckets(merged["latency_buckets"], r.latency_buckets)
        merged["ttft_sum_ms"] += r.ttft_sum_ms or 0
        merged["ttft_count"] += r.ttft_count or 0
        _merge_buckets(merged["ttft_buckets"], r.ttft_buckets)
        merged["in_flight_peak"] = max(merged["in_flight_peak"], r.in_flight_peak or 0)
    return merged


def _merge_buckets(target: list[int], source: list | None) -> None:
    """按位累加直方图桶数组；source 缺位/短数组按 0 补齐（老行/空数组不截断结果）。"""
    if not source:
        return
    for i, val in enumerate(source):
        if i >= len(target):
            break
        target[i] += val or 0


def summarize(merged: dict) -> dict:
    """合并视图 → 曲线点指标（速率类由调用方按 bucket 宽度换算）。

    requests 含 rejected（拒绝也是「到达过网关」的请求量，时间轴总量口径）；
    error_rate 分母只取「触达上游」的请求（requests − rejected）——401 刷库
    攻击会把 rejected 抬高，混进分母会把真实错误率稀释成「健康假象」。
    """
    reqs = merged["requests"]
    forwarded = reqs - merged["rejected"]
    return {
        "requests": reqs,
        "errors": merged["errors"],
        "interrupted": merged["interrupted"],
        "rejected": merged["rejected"],
        "error_rate": round(merged["errors"] / forwarded, 4) if forwarded > 0 else 0.0,
        "total_tokens": merged["total_tokens"],
        "prompt_tokens": merged["prompt_tokens"],
        "completion_tokens": merged["completion_tokens"],
        "cached_tokens": merged["cached_tokens"],
        "stream_requests": merged["stream_requests"],
        "retry_count": merged["retry_count"],
        "latency_avg_ms": _latency_avg_ms(merged),
        "latency_p50_ms": percentile_from_buckets(merged["latency_buckets"], LATENCY_BUCKET_BOUNDS, 0.50),
        "latency_p95_ms": percentile_from_buckets(merged["latency_buckets"], LATENCY_BUCKET_BOUNDS, 0.95),
        "latency_p99_ms": percentile_from_buckets(merged["latency_buckets"], LATENCY_BUCKET_BOUNDS, 0.99),
        "ttft_avg_ms": _ttft_avg_ms(merged),
        "ttft_p95_ms": percentile_from_buckets(merged["ttft_buckets"], LATENCY_BUCKET_BOUNDS, 0.95),
        "in_flight_peak": merged["in_flight_peak"],
    }


def _forwarded(merged: dict) -> int:
    """触达上游的请求数 = 总请求 − 拒绝（success + error + interrupted）。"""
    return merged["requests"] - merged["rejected"]


def _error_rate(merged: dict) -> float:
    """错误率 = error / 触达上游请求（拒绝不参与，interrupted 参与分母不计错误）。"""
    forwarded = _forwarded(merged)
    return round(merged["errors"] / forwarded, 4) if forwarded > 0 else 0.0


def _ttft_avg_ms(merged: dict) -> int | None:
    """平均首帧延迟（仅流式；无样本返回 None）。"""
    return round(merged["ttft_sum_ms"] / merged["ttft_count"]) if merged["ttft_count"] else None


def _latency_avg_ms(merged: dict) -> int | None:
    """平均端到端延迟（无样本返回 None）。"""
    return round(merged["latency_sum_ms"] / merged["latency_count"]) if merged["latency_count"] else None


def _as_local(dt: datetime | None, default: datetime | None = None) -> datetime | None:
    """入参 datetime 归一为系统本地时区（naive 视为本地）。"""
    if dt is None:
        return default
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_local_tz())
    return dt


def _range_conditions(from_: datetime, to: datetime, dim: str | None, dim_id: str | int | None):
    """窗口条件 + 可选的单维度收敛（维度下钻用：dim 为必选过滤时不走这里）。"""
    conds = [MetricRollup.bucket_start >= from_, MetricRollup.bucket_start < to]
    if dim is not None:
        col = _DIMENSION_COLUMNS[dim]
        conds.append(col == dim_id)
    return conds


async def _load_rows(
    db: AsyncSession,
    from_: datetime,
    to: datetime,
    tenant_id: int | None = None,
    model_alias: str | None = None,
) -> list[MetricRollup]:
    """同步取窗口内全部桶行（调用方已保证 from/to 有效）。

    ``tenant_id`` 按租户收敛（租户端看板/下钻用），NULL 行（平台 Key）自然排除；
    ``model_alias`` 按模型别名收敛（大屏筛选下推）。两个可选过滤同构追加——
    空值（含 ``?model=`` 解析出的空串）视为未过滤，不当死条件下推。
    """
    conds = [MetricRollup.bucket_start >= from_, MetricRollup.bucket_start < to]
    if tenant_id is not None:
        conds.append(MetricRollup.tenant_id == tenant_id)
    if model_alias:
        conds.append(MetricRollup.model_alias == model_alias)
    return list((await db.execute(select(MetricRollup).where(*conds))).scalars())


async def _node_counts(db: AsyncSession) -> tuple[int, int]:
    """在线节点数 / 节点总数（来自 Node 表，与指标时间窗无关）。"""
    online = (await db.execute(select(func.count(Node.id)).where(Node.status == NodeStatus.ONLINE))).scalar() or 0
    total = (await db.execute(select(func.count(Node.id)))).scalar() or 0
    return int(online), int(total)


def summarize_core_rows(rows: list[MetricRollup]) -> dict:
    """桶行 → 指标核心（窗口聚合本体；供 summary_core 与已自行取行的调用方共用）。"""
    merged = merge_bucket_rows(rows)
    active_models = len({r.model_alias for r in rows if r.model_alias and r.status in FORWARDED_STATUSES})
    active_tenants = len({r.tenant_id for r in rows if r.tenant_id is not None and r.status in FORWARDED_STATUSES})
    return {
        "calls": _forwarded(merged),
        "active_models": active_models,
        "active_tenants": active_tenants,
        "avg_ttft_ms": _ttft_avg_ms(merged),
        "error_rate": _error_rate(merged),
        "total_tokens": merged["total_tokens"],
    }


# 空窗零值（窗口无效时的兜底形状）：由 summarize_core_rows([]) 派生，键集与
# summary_core 永远一致——新增指标只改 summarize_core_rows 一处
ZERO_CORE = summarize_core_rows([])


async def summary_core(
    db: AsyncSession,
    *,
    from_: datetime,
    to: datetime,
    tenant_id: int | None = None,
) -> dict | None:
    """窗口内指标核心（不含节点在线态）：供 summary 与两端大屏 screen 共用。

    调用口径 = 触达上游（success + error + interrupted），拒绝不参与；
    窗口无效（含空窗）返回 None，由调用方决定兜底与是否补查节点计数。
    ``tenant_id`` 非空时按租户收敛（租户端看板）。
    """
    from_ = _as_local(from_)
    to = _as_local(to)
    if from_ is None or to is None or to <= from_:
        return None
    rows = await _load_rows(db, from_, to, tenant_id=tenant_id)
    return summarize_core_rows(rows)


async def summary(db: AsyncSession, *, from_: datetime, to: datetime) -> dict:
    """数据看板顶部摘要卡：给定时间窗内调用数/活跃模型/平均 TTFT/错误率/Token + 节点在线态。

    调用口径 = 触达上游（success + error + interrupted），拒绝不参与；
    平均延迟 = TTFT 均值（仅流式）；在线节点为实时值，不受时间窗约束
    （窗口无效时全卡归 0，不查节点）。
    """
    core = await summary_core(db, from_=from_, to=to)
    if core is None:
        return {**ZERO_CORE, "online_nodes": 0, "total_nodes": 0}
    online, total = await _node_counts(db)
    return {**core, "online_nodes": online, "total_nodes": total}


def trend_day_list(days: int, now: datetime) -> list:
    """趋势的逐日 X 轴：近 N 天**含今天**共 N 个点（空日由调用方补 0）。

    与 tenants/overview 的窗口同口径（``now - (days-1)`` 起）——此前是「往回 N 天
    + 今天」N+1 个点，同一 days 参数两接口点数差一，前端并排画图会错位。
    """
    days = max(1, days)
    start_day = (now - timedelta(days=days - 1)).date()
    return [start_day + timedelta(days=i) for i in range(days)]


def trend_from_rows(rows: list[MetricRollup], day_list: list) -> list[dict]:
    """桶行 → 逐日趋势点（day_list 外的行不计；空日补 0）。"""
    by_day: dict = {}
    for r in rows:
        day = r.bucket_start.astimezone().date()
        by_day.setdefault(day, []).append(r)

    result = []
    for day in day_list:
        merged = merge_bucket_rows(by_day.get(day, []))
        result.append(
            {
                "date": day.isoformat(),
                "call_count": _forwarded(merged),
                "error_rate": _error_rate(merged),
            }
        )
    return result


async def trend(db: AsyncSession, *, days: int = 14, tenant_id: int | None = None) -> list[dict]:
    """近 N 天调用趋势：逐日调用量 + 错误率（空日补 0）。

    语义为近 N 天**含今天**共 N 个点（与 tenants/overview 同口径）。
    ``tenant_id`` 非空时按租户收敛（租户端看板）。
    """
    to = now_local()
    day_list = trend_day_list(days, to)
    from_ = to.replace(
        year=day_list[0].year,
        month=day_list[0].month,
        day=day_list[0].day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    rows = await _load_rows(db, from_, to, tenant_id=tenant_id)
    return trend_from_rows(rows, day_list)


async def _model_truncation_rates(
    db: AsyncSession, from_: datetime, to: datetime, *, model: str | None = None, tenant_id: int | None = None
) -> dict[str, float]:
    """窗口内按模型别名的截断率（分母 = 该模型「结束原因已知」的落账行数）。

    表体读 rollup（桶聚合、有索引），但「被截断」这个布尔只在逐请求的
    ``request_log`` 上——单独一条 GROUP BY 按 model_alias 聚合（结果是模型数
    量级的小集合），调用方按名字回填。拒绝不落账，故不在分母里。

    ``model`` / ``tenant_id`` 必须与表体查询用同一组筛选：否则行是租户口径、
    截断率是全平台口径，一行里两个数来源不同（截断率还自带租户索引）。

    分母只数**结束原因已知**的行：上游没给（或本列上线前的历史行）不计入，
    否则那些行会把率稀释——空窗（无人上报结束原因）返回不出值而不是 0。
    """
    conds = [RequestLog.created_at >= from_, RequestLog.created_at < to]
    if model:
        conds.append(RequestLog.model_alias == model)
    if tenant_id is not None:
        conds.append(RequestLog.tenant_id == tenant_id)
    rows = (
        await db.execute(
            select(
                RequestLog.model_alias,
                func.count().filter(RequestLog.finish_reason.isnot(None)).label("known"),
                func.count().filter(RequestLog.finish_reason.in_(TRUNCATED_FINISH_REASONS)).label("truncated"),
            )
            .where(*conds)
            .group_by(RequestLog.model_alias)
        )
    ).all()
    rates: dict[str, float] = {}
    for row in rows:
        rate = truncation_rate(int(row.truncated or 0), int(row.known or 0))
        if rate is not None:
            rates[row.model_alias] = rate
    return rates


async def model_ranking(
    db: AsyncSession,
    *,
    from_: datetime,
    to: datetime,
    top_n: int = 10,
    model: str | None = None,
    tenant_id: int | None = None,
) -> list[dict]:
    """模型统计表：按调用量排序的 top N，含占比/平均延迟/平均 TTFT/错误率/截断率/Token。

    ``model`` / ``tenant_id`` 为可选筛选（下推 SQL，而非取回后再丢行）。截断率
    来自 request_log（rollup 没有这个维度），窗口内无落账行为 None。
    """
    from_ = _as_local(from_)
    to = _as_local(to)
    if from_ is None or to is None or to <= from_:
        return []
    rows = await _load_rows(db, from_, to, tenant_id=tenant_id, model_alias=model)
    if not rows:
        return []  # 空窗早退：截断率那条扫描回填谁都用不上，不必白跑
    truncation = await _model_truncation_rates(db, from_, to, model=model, tenant_id=tenant_id)

    grouped: dict[str, list] = {}
    for r in rows:
        key = r.model_alias or "(unknown)"
        grouped.setdefault(key, []).append(r)

    total_forwarded = sum(_forwarded(merge_bucket_rows(g)) for g in grouped.values())
    items = []
    for model_name, group in grouped.items():
        merged = merge_bucket_rows(group)
        calls = _forwarded(merged)
        items.append(
            {
                "model": model_name if model_name != "(unknown)" else None,
                "call_count": calls,
                "ratio": round(calls / total_forwarded, 4) if total_forwarded else 0.0,
                "avg_latency_ms": _latency_avg_ms(merged),
                "avg_ttft_ms": _ttft_avg_ms(merged),
                "error_rate": _error_rate(merged),
                "truncation_rate": truncation.get(model_name),
                "tokens": merged["total_tokens"],
            }
        )
    items.sort(key=lambda x: x["call_count"], reverse=True)
    return items[:top_n]


async def tenant_ranking(db: AsyncSession, *, from_: datetime, to: datetime, top_n: int = 10) -> list[dict]:
    """租户统计表：按调用量排序的 top N（含租户名），平台 Key（tenant_id 空）不计入。"""
    from_ = _as_local(from_)
    to = _as_local(to)
    if from_ is None or to is None or to <= from_:
        return []
    rows = await _load_rows(db, from_, to)

    grouped: dict[int, list] = {}
    for r in rows:
        if r.tenant_id is None:
            continue  # 平台 Key 无归属租户，不进租户排名
        grouped.setdefault(r.tenant_id, []).append(r)

    # 名字只查在榜租户（全表拉取与排名窗口大小无关地放大）
    tenant_rows = list((await db.execute(select(Tenant).where(Tenant.id.in_(list(grouped))))).scalars())
    name_by_id = {t.id: t.name for t in tenant_rows}

    total_forwarded = sum(_forwarded(merge_bucket_rows(g)) for g in grouped.values())
    items = []
    for tid, group in grouped.items():
        merged = merge_bucket_rows(group)
        calls = _forwarded(merged)
        items.append(
            {
                "tenant_id": tid,
                "tenant": name_by_id.get(tid, f"租户#{tid}"),
                "call_count": calls,
                "ratio": round(calls / total_forwarded, 4) if total_forwarded else 0.0,
            }
        )
    items.sort(key=lambda x: x["call_count"], reverse=True)
    return items[:top_n]


async def dimension_series(
    db: AsyncSession,
    *,
    dim: str,
    dim_id: str | int,
    from_: datetime,
    to: datetime,
    granularity: str = "auto",
) -> list[dict]:
    """单维度成员（部署等）的时间序列曲线。"""
    from_ = _as_local(from_)
    to = _as_local(to)
    if from_ is None or to is None or to <= from_ or dim not in _DIMENSION_COLUMNS:
        return []
    gran = (
        granularity
        if granularity in GRANULARITY_MINUTES
        else pick_granularity(from_, to, hourly_span_limit_seconds=_DEPLOYMENT_HOUR_MAX_SPAN_SECONDS)
    )
    step = timedelta(minutes=GRANULARITY_MINUTES[gran])
    conds = _range_conditions(from_, to, dim, dim_id)
    rows = list((await db.execute(select(MetricRollup).where(*conds))).scalars())
    # 桶按**本地**边界取整：5m/1h 在整小时时区下与 epoch 对齐等价，日桶不是
    # （epoch 对齐的「日」切在 UTC 零点 = 本地早上八点）。与 SQL 侧
    # bucket_floor_expr 同一口径，两处对新旧路径必须给同一天界。
    tz_off = local_offset_seconds()
    width = int(step.total_seconds())
    by_slot: dict[datetime, list[MetricRollup]] = {}
    for r in rows:
        slot_epoch = int(r.bucket_start.timestamp()) + tz_off
        slot_epoch -= slot_epoch % width
        by_slot.setdefault(datetime.fromtimestamp(slot_epoch - tz_off, tz=_local_tz()), []).append(r)
    window_seconds = step.total_seconds()
    series = []
    for slot in sorted(by_slot):
        merged = merge_bucket_rows(by_slot[slot])
        point = {"ts": slot.isoformat(), "granularity": gran, "window_seconds": int(window_seconds)}
        point.update(summarize(merged))
        point["qps"] = round(merged["requests"] / window_seconds, 3) if window_seconds else 0.0
        point["tps"] = round(merged["total_tokens"] / window_seconds, 3) if window_seconds else 0.0
        point["tpm"] = round(merged["total_tokens"] / (window_seconds / 60), 1) if window_seconds else 0.0
        series.append(point)
    return series


# ---- Token 汇总（/dashboard/tokens；表格不需要实时，走 5min 桶冷层） ----


def token_rows_from_agg(rows: list, top: int = 5, names: dict | None = None) -> dict:
    """SQL 聚合行（dim_key + 四项 token 和）→ Token 汇总表（rows + other + 合计）。

    口径必须唯一确定：``total_tokens = input + output``；``cached_tokens`` 是
    ``input_tokens`` 的子集（OpenAI 系 usage 的 prompt_tokens 本身已含缓存命中），
    列语义是「其中缓存」，不参与合计算式。ratio 分母 = 全量 total_tokens，
    rows + other 的 ratio 之和为 1。
    """
    names = names or {}
    agg: dict = {}
    for r in rows:
        key = r.dim_key
        if key is None or key == "":
            continue
        bucket = agg.setdefault(key, {"input": 0, "output": 0, "cached": 0, "total": 0})
        bucket["input"] += r.prompt_tokens or 0
        bucket["output"] += r.completion_tokens or 0
        bucket["cached"] += r.cached_tokens or 0
        bucket["total"] += r.total_tokens or 0
    grand = {"input": 0, "output": 0, "cached": 0, "total": 0}
    for bucket in agg.values():
        for field in grand:
            grand[field] += bucket[field]
    ranked = sorted(agg, key=lambda k: agg[k]["total"], reverse=True)

    def _row(key) -> dict:
        bucket = agg[key]
        return {
            "key": names.get(key, str(key)),
            "input_tokens": bucket["input"],
            "output_tokens": bucket["output"],
            "cached_tokens": bucket["cached"],
            "total_tokens": bucket["total"],
            "ratio": round(bucket["total"] / grand["total"], 4) if grand["total"] else 0.0,
        }

    out_rows = [_row(k) for k in ranked[: max(1, top)]]
    rest = ranked[max(1, top) :]
    other = None
    if rest:
        other = {"key": "__other__", "count": len(rest)}
        for field in ("input", "output", "cached", "total"):
            other[f"{field}_tokens"] = sum(agg[k][field] for k in rest)
        other["ratio"] = round(other["total_tokens"] / grand["total"], 4) if grand["total"] else 0.0
    return {
        "rows": out_rows,
        "other": other,
        "total": {
            "input_tokens": grand["input"],
            "output_tokens": grand["output"],
            "cached_tokens": grand["cached"],
            "total_tokens": grand["total"],
        },
    }


async def _token_dim_names(db: AsyncSession, dim: str, dim_keys: list) -> dict:
    """维度成员的显示名解析（模型=别名本身；其余维度 join 业务表取名）。"""
    if dim not in DASHBOARD_DIMS or dim == "model":
        return {}
    raw_keys = {k for k in dim_keys if k is not None and k != ""}
    if not raw_keys:
        return {}
    try:
        if dim == "tenant":
            tenants = list((await db.execute(select(Tenant).where(Tenant.id.in_(list(raw_keys))))).scalars())
            return {t.id: t.name for t in tenants}
        if dim == "api_key":
            from llmpod.models import ApiKey

            keys = list((await db.execute(select(ApiKey).where(ApiKey.id.in_(list(raw_keys))))).scalars())
            return {k.id: k.name for k in keys}
        if dim == "deployment":
            from llmpod.models import ModelDeployment

            deps = list(
                (
                    await db.execute(
                        select(ModelDeployment.deployment_id, ModelDeployment.name).where(
                            ModelDeployment.deployment_id.in_(list(raw_keys))
                        )
                    )
                ).all()
            )
            return {d.deployment_id: d.name for d in deps}
    except Exception:
        return {}  # 名称解析失败退回原始 key，不影响数值
    return {}


async def token_summary(
    db: AsyncSession,
    *,
    dim: str,
    from_: datetime | None,
    to: datetime | None,
    top: int = 5,
    model: str | None = None,
    tenant_id: int | None = None,
) -> dict:
    """Token 汇总入口：SQL 侧按维度列 SUM 聚合窗口桶行（表格不需要实时，冷层即可）。

    聚合下推 SQL 而非逐行拉桶行再 Python 求和：默认「今日」窗口在桶 × 标签
    组合规模下是数十万行（含两个没人读的 13 元素直方图数组列），而这个查询
    与 REST/WS 同进程——结果最多几百个数字，不该为它搬整窗原始行。
    ``model`` / ``tenant_id`` 为维度列之外的筛选（下推 where；dim=model 时
    model 筛选等价于只看该成员，同样合法）。
    """
    from_ = _as_local(from_)
    to = _as_local(to)
    to = to or now_local()
    from_ = from_ or to.replace(hour=0, minute=0, second=0, microsecond=0)
    if dim not in DASHBOARD_DIMS or to <= from_:
        return {
            "from": from_.isoformat(),
            "to": to.isoformat(),
            "rows": [],
            "other": None,
            "total": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0},
        }
    col = _DIMENSION_COLUMNS[dim]
    m = MetricRollup
    conds = [m.bucket_start >= from_, m.bucket_start < to, col.isnot(None)]
    if model:  # 空串视为未过滤（?model= 不该变成 model_alias='' 的死条件）
        conds.append(m.model_alias == model)
    if tenant_id is not None:
        conds.append(m.tenant_id == tenant_id)
    rows = (
        await db.execute(
            select(
                col.label("dim_key"),
                func.sum(m.prompt_tokens).label("prompt_tokens"),
                func.sum(m.completion_tokens).label("completion_tokens"),
                func.sum(m.total_tokens).label("total_tokens"),
                func.sum(m.cached_tokens).label("cached_tokens"),
            )
            .where(*conds)
            .group_by(col)
        )
    ).all()
    names = await _token_dim_names(db, dim, [r.dim_key for r in rows])
    return {"from": from_.isoformat(), "to": to.isoformat(), **token_rows_from_agg(rows, top=top, names=names)}


# ---- 上游聚合（屏1 /overview 与 屏3 /anomaly 共用的 rollup 查询） ----


async def upstream_type_rows(db: AsyncSession, from_: datetime, to: datetime) -> list:
    """窗口内按 upstream_type（local / external）聚合：requests / errors / rejected / 延迟。

    errors / rejected 由 FILTER 拆好；latency 两列供调用方算窗口平均延迟
    （qpm / success_rate 由各端点按自己的窗口语义换算）。
    """
    m = MetricRollup
    return list(
        (
            await db.execute(
                select(
                    m.upstream_type.label("upstream_type"),
                    func.sum(m.requests).label("requests"),
                    func.sum(m.requests).filter(m.status == "error").label("errors"),
                    func.sum(m.requests).filter(m.status == "rejected").label("rejected"),
                    func.sum(m.latency_sum_ms).label("latency_sum_ms"),
                    func.sum(m.latency_count).label("latency_count"),
                )
                .where(m.bucket_start >= from_, m.bucket_start < to, m.upstream_type.isnot(None))
                .group_by(m.upstream_type)
            )
        ).all()
    )


# 端点被删后 model_endpoint 行随之消失，LEFT JOIN 取不到名字的历史桶统一归这一行
# （前端直接渲染该文案；不归行就会让明细合计小于「外部上游」汇总卡）。
# 覆盖面上它还兜住 endpoint_id 为空的外部桶——该组合在当前写入路径下不可达
# （拒绝不落库、落库路径的 target 恒非 None），故文案按「已删除」写即可。
_DELETED_ENDPOINT_LABEL = "已删除端点"


async def upstream_endpoint_rows(db: AsyncSession, from_: datetime, to: datetime) -> list:
    """窗口内外部流量按上游**端点**聚合（endpoint_id 逻辑关联 ModelEndpoint 取展示名 name）。

    只看 external：本地上游就是自家集群，local/external 汇总卡已覆盖，明细只列外部端点。

    两处口径是刻意的，都为「明细不丢行」：

    - 取 ``ModelEndpoint.name`` 而非 ``provider_name``：后者选填，这里过去用
      ``provider_name IS NOT NULL`` 过滤，**没填供应商的外部端点会整个从明细消失**
      （请求量仍进「外部上游」汇总卡，两边对不上）。``name`` 恒非空（本地=部署名、
      外部=登记名），正是要展示给用户的名字。
    - ``outerjoin`` 而非 ``join``：端点被删后其历史桶 join 不上会整批掉行，同样造成
      明细合计小于汇总卡；掉行归入 ``_DELETED_ENDPOINT_LABEL`` 兜底行。
    """
    from llmpod.models import ModelEndpoint

    m = MetricRollup
    # 同一表达式同时用于 SELECT 与 GROUP BY：PG 侧渲染成 coalesce(...)，不依赖 label 名
    upstream_name = func.coalesce(ModelEndpoint.name, _DELETED_ENDPOINT_LABEL)
    return list(
        (
            await db.execute(
                select(
                    upstream_name.label("endpoint_name"),
                    func.sum(m.requests).label("requests"),
                    func.sum(m.requests).filter(m.status == "error").label("errors"),
                    func.sum(m.requests).filter(m.status == "rejected").label("rejected"),
                    func.sum(m.latency_sum_ms).label("latency_sum_ms"),
                    func.sum(m.latency_count).label("latency_count"),
                )
                .outerjoin(ModelEndpoint, ModelEndpoint.id == m.endpoint_id)
                .where(
                    m.bucket_start >= from_,
                    m.bucket_start < to,
                    m.upstream_type == "external",
                )
                .group_by(upstream_name)
            )
        ).all()
    )
