"""租户端数据看板 service：控制台整屏一次聚合（指标卡 + 趋势 + 可用模型 + 最近调用 + 余额/月消费）。

数据源全部复用既有域：metric_rollup（经 metric_query_service 的租户收敛查询，指标卡
与趋势共用一次取行）、model_block_service（可用模型，与 /tenant/models 同口径）、
request_log（最近调用前 N 条，排序与 request_log 域一致）、wallet_service（余额三段
与当月消费汇总）。本层只做聚合。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.models import RequestLog, Route
from llmpod.services import metric_query_service
from llmpod.services.model_block_service import active_blocked_route_ids
from llmpod.services.request_log_service import RECENCY_ORDER
from llmpod.services.wallet_service import consume_total, wallet_snapshot
from llmpod.utils.time import format_datetime, now_local

# 控制台整屏的固定装配参数（单请求喂饱，不做 knobs）。
# trend(days=N) 语义 = 近 N 天含今天（N 个点），取 7 得「近 7 日」
_TREND_DAYS = 7
_RECENT_CALLS = 5


async def tenant_screen(db: AsyncSession, tenant_id: int, *, from_: datetime | None, to: datetime | None) -> dict:
    """组装租户控制台整屏。指标卡受时间窗约束（缺省今日 00:00 本地 → 当前），
    模型/最近调用/余额/月消费为实时或当月值。
    """
    from_, to = metric_query_service.resolve_day_window(from_, to)
    now = now_local()
    # 指标卡与趋势共用一次取行：窗口取并集（趋势恒为「近 7 日到当前」，与卡片窗口
    # 无关；默认今日窗是趋势窗的子集），卡片子集在内存里再切
    day_list = metric_query_service.trend_day_list(_TREND_DAYS, now)
    trend_from = now.replace(
        year=day_list[0].year,
        month=day_list[0].month,
        day=day_list[0].day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    rows = await metric_query_service.load_rows(db, min(from_, trend_from), max(to, now), tenant_id=tenant_id)
    if to <= from_:
        metrics = metric_query_service.ZERO_CORE
    else:
        # 取行上界是 max(to, now)（趋势要取到当前），指标卡须自带上界切回 [from_, to)
        metrics = metric_query_service.summarize_core_rows([r for r in rows if from_ <= r.bucket_start < to])
    trend = metric_query_service.trend_from_rows(rows, day_list)

    return {
        **metrics,
        "trend": trend,
        "models": await _available_models(db, tenant_id),
        "recent_calls": await _recent_calls(db, tenant_id),
        **await _finance(db, tenant_id),
    }


async def _available_models(db: AsyncSession, tenant_id: int) -> list[dict]:
    """本租户可用模型（published − 生效封禁；封禁集经 model_block_service 与
    GET /tenant/models 同源）。"""
    blocked_ids = await active_blocked_route_ids(db, tenant_id)
    routes = (await db.execute(select(Route.id, Route.name).where(Route.status == "published"))).all()
    return [{"id": rid, "name": name} for rid, name in routes if rid not in blocked_ids]


async def _recent_calls(db: AsyncSession, tenant_id: int) -> list[dict]:
    """最近调用（前 N 条计量行摘要；排序复用 request_log 域的 RECENCY_ORDER，
    拒绝不落 request_log，天然只含真实转发）。"""
    rows = (
        await db.execute(
            select(
                RequestLog.model_alias,
                RequestLog.prompt_tokens,
                RequestLog.completion_tokens,
                RequestLog.latency_ms,
                RequestLog.status,
                RequestLog.created_at,
            )
            .where(RequestLog.tenant_id == tenant_id)
            .order_by(*RECENCY_ORDER)
            .limit(_RECENT_CALLS)
        )
    ).all()
    return [
        {
            "model": model,
            "input_tokens": prompt,
            "output_tokens": completion,
            "latency_ms": latency,
            "status": status,
            "created_at": format_datetime(created_at),
        }
        for model, prompt, completion, latency, status, created_at in rows
    ]


async def _finance(db: AsyncSession, tenant_id: int) -> dict:
    """余额三段 + 本月消费（口径归 wallet_service：三段/消费汇总唯一定义处）。"""
    balance, frozen, available = await wallet_snapshot(db, tenant_id)
    month_start = now_local().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "balance": balance,
        "frozen_balance": frozen,
        "available": available,
        "month_consume": await consume_total(db, tenant_id, month_start),
    }
