"""平台总览屏服务：今日 vs 昨日同期双窗 KPI + 小时双线趋势 + 上游构成 + 服务状态。

屏 1 是固定日历窗（本地零点 → 现在），对照恒为昨日同期（今天 10 点 vs 昨天 10 点）
——平台运营看「比昨天同时刻」，滚动环比在日内周期性流量下会错位。屏 2 的区间由
前端给出，对照窗也一并由前端给（今日预设给的就是昨日同期，与屏 1 重合）。

数据源：KPI/趋势走 metric_rollup（两窗合并一次查询——按索引列 bucket_start
分组，窗侧归属与小时槽在 Python 侧从桶起点推导；活跃模型/租户的去重计数单独
一条 GROUP BY，逐桶去重数不可跨桶相加）；上游构成复用 metric_query_service 的
upstream 聚合；服务状态（节点/GPU/集群/部署）直接调 dashboard_screen_service.
service_panel，不复制实现。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.manager.deployment.constants import DeploymentStatus
from llmpod.config.constants import PLATFORM_TENANT_ID
from llmpod.models import Instance, MetricRollup, ModelDeployment, Tenant
from llmpod.services import dashboard_screen_service as screen_svc
from llmpod.services import metric_query_service as mq
from llmpod.services.dashboard_realtime_service import _delta_pct
from llmpod.services.metric_query_service import FORWARDED_STATUSES, upstream_success_rate
from llmpod.utils.time import now_local


def _delta_abs(cur: int, prev: int | None) -> int | None:
    if prev is None:
        return None
    return cur - prev


async def load_bucket_rows(db: AsyncSession, yesterday_from: datetime, to: datetime) -> list:
    """[昨日零点, 现在) 一次查询 → 按 bucket_start（索引列）分组的聚合行。

    calls 口径 = 触达上游（success+error+interrupted），拒绝排除；窗侧归属与
    小时槽由调用方从桶起点推导（split_dual_rows / hourly_dual_trend）。
    """
    m = MetricRollup
    return list(
        (
            await db.execute(
                select(
                    m.bucket_start.label("bucket_start"),
                    func.sum(m.requests).filter(m.status.in_(FORWARDED_STATUSES)).label("calls"),
                    func.sum(m.requests).filter(m.status == "error").label("errors"),
                    func.sum(m.requests).filter(m.status != "rejected").label("forwarded"),
                    func.sum(m.ttft_sum_ms).label("ttft_sum_ms"),
                    func.sum(m.ttft_count).label("ttft_count"),
                )
                .where(m.bucket_start >= yesterday_from, m.bucket_start < to)
                .group_by(m.bucket_start)
            )
        ).all()
    )


def split_dual_rows(rows: list, today_from: datetime, yesterday_cutoff: datetime) -> tuple[list, list]:
    """桶聚合行 → (今日行, 昨日同期行)。

    ``yesterday_cutoff`` = 昨日同时刻：晚于它的昨日行属于「今日还没走到的
    时段」，不进对照窗——否则「今日 11h vs 昨日 24h」的 delta 全线失真。
    """
    today_rows = [r for r in rows if r.bucket_start >= today_from]
    yesterday_rows = [r for r in rows if r.bucket_start < today_from and r.bucket_start < yesterday_cutoff]
    return today_rows, yesterday_rows


async def load_active_counts(
    db: AsyncSession, yesterday_from: datetime, yesterday_to: datetime, today_from: datetime, to: datetime
) -> dict[str, dict[str, int]]:
    """两窗的活跃模型/租户去重计数（整窗一条 GROUP BY——逐桶去重数不可相加）。

    返回 {"today": {...}, "yesterday": {...}}；昨日窗只统计到昨日同时刻
    （yesterday_to），之后的行直接丢弃（分组侧用 CASE 三分：today/yesterday/skip）。
    """
    m = MetricRollup
    side = case(
        (m.bucket_start >= today_from, "today"),
        (m.bucket_start < yesterday_to, "yesterday"),
        else_="skip",
    )
    rows = (
        await db.execute(
            select(
                side.label("side"),
                func.count(func.distinct(m.model_alias))
                .filter(m.status.in_(FORWARDED_STATUSES), m.model_alias.isnot(None))
                .label("models"),
                func.count(func.distinct(m.tenant_id))
                .filter(m.status.in_(FORWARDED_STATUSES), m.tenant_id.isnot(None))
                .label("tenants"),
            )
            .where(m.bucket_start >= yesterday_from, m.bucket_start < to)
            .group_by(side)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        if r.side != "skip":
            out[r.side] = {"models": int(r.models or 0), "tenants": int(r.tenants or 0)}
    return out


def hourly_dual_trend(today_rows: list, yesterday_rows: list, today_from: datetime) -> dict:
    """两窗桶行 → 小时粒度双线点（今日 calls vs 昨日同期 calls）。

    小时槽 = 桶距各自窗口零点的小时数（今日 10 点与昨日 10 点同槽）；X 轴
    小时集合取今日窗——昨日的「还没走到」时段不画，对照线尾部短一截是预期形态。
    """
    today_by_hour: dict[int, int] = {}
    yesterday_by_hour: dict[int, int] = {}
    for r in today_rows:
        hour = int((r.bucket_start - today_from).total_seconds() // 3600)
        today_by_hour[hour] = today_by_hour.get(hour, 0) + int(r.calls or 0)
    for r in yesterday_rows:
        hour = int((r.bucket_start - (today_from - timedelta(days=1))).total_seconds() // 3600)
        yesterday_by_hour[hour] = yesterday_by_hour.get(hour, 0) + int(r.calls or 0)
    hours = sorted(today_by_hour.keys())
    points = [
        {
            "hour": h,
            "calls": today_by_hour.get(h, 0),
            "calls_yesterday": yesterday_by_hour.get(h, 0),
        }
        for h in hours
    ]
    return {
        "points": points,
        "today_total": sum(today_by_hour.values()),
        "yesterday_same_period_total": sum(yesterday_by_hour.get(h, 0) for h in hours),
    }


def _window_sums(rows: list) -> dict:
    """单窗聚合行求和（KPI 分子分母原料；calls/errors/forwarded/ttft 四类）。"""
    out = {"calls": 0, "errors": 0, "forwarded": 0, "ttft_sum_ms": 0, "ttft_count": 0}
    for r in rows:
        out["calls"] += int(r.calls or 0)
        out["errors"] += int(r.errors or 0)
        out["forwarded"] += int(r.forwarded or 0)
        out["ttft_sum_ms"] += int(r.ttft_sum_ms or 0)
        out["ttft_count"] += int(r.ttft_count or 0)
    return out


def overview_kpis(today: dict, yesterday: dict) -> dict:
    """双窗原料 → KPI 卡。昨日无数据时 delta 全部置 null（不猜）。"""

    def _success_rate(acc: dict) -> float | None:
        return round((acc["forwarded"] - acc["errors"]) / acc["forwarded"], 4) if acc["forwarded"] > 0 else None

    def _ttft(acc: dict) -> int | None:
        return round(acc["ttft_sum_ms"] / acc["ttft_count"]) if acc["ttft_count"] else None

    cur_rate, prev_rate = _success_rate(today), _success_rate(yesterday)
    cur_ttft, prev_ttft = _ttft(today), _ttft(yesterday)
    has_yesterday = yesterday["calls"] > 0 or yesterday["forwarded"] > 0
    return {
        "calls": {
            "value": today["calls"],
            "delta_pct": _delta_pct(today["calls"], yesterday["calls"]) if has_yesterday else None,
        },
        "active_tenants": {
            "value": today.get("tenants", 0),
            "delta": _delta_abs(today.get("tenants", 0), yesterday.get("tenants")) if has_yesterday else None,
        },
        "active_models": {
            "value": today.get("models", 0),
            "delta": _delta_abs(today.get("models", 0), yesterday.get("models")) if has_yesterday else None,
        },
        "success_rate": {
            "value": cur_rate,
            "delta_pp": round((cur_rate - prev_rate) * 100, 2)
            if cur_rate is not None and prev_rate is not None
            else None,
        },
        "avg_ttft_ms": {
            "value": cur_ttft,
            "delta_ms": cur_ttft - prev_ttft if cur_ttft is not None and prev_ttft is not None else None,
        },
    }


async def _upstream_overview(db: AsyncSession, from_: datetime, to: datetime) -> dict:
    """今日窗上游构成：local/external 份额 + 外部端点份额（share 分母 = 今日上游请求总量）。"""
    type_rows = await mq.upstream_type_rows(db, from_, to)
    # 本地-only 部署的常态：没有 external 流量就不必再扫一遍端点 join
    has_external = any(r.upstream_type == "external" for r in type_rows)
    endpoint_rows = await mq.upstream_endpoint_rows(db, from_, to) if has_external else []
    total = sum(int(r.requests or 0) for r in type_rows)

    def _card(rows: list) -> dict:
        requests = sum(int(r.requests or 0) for r in rows)
        return {
            "share": round(requests / total, 4) if total > 0 else 0.0,
            "success_rate": upstream_success_rate(
                requests, sum(int(r.errors or 0) for r in rows), sum(int(r.rejected or 0) for r in rows)
            ),
        }

    providers = []
    for r in sorted(endpoint_rows, key=lambda x: int(x.requests or 0), reverse=True):
        requests = int(r.requests or 0)
        providers.append(
            {
                "name": r.endpoint_name,
                "share": round(requests / total, 4) if total > 0 else 0.0,
                "success_rate": upstream_success_rate(requests, int(r.errors or 0), int(r.rejected or 0)),
            }
        )
    return {
        "local": _card([r for r in type_rows if r.upstream_type == "local"]),
        "external": _card([r for r in type_rows if r.upstream_type == "external"]),
        "providers": providers,
    }


async def _new_tenants(db: AsyncSession, today_from: datetime) -> dict:
    """今日新建租户数 / 本周（近 7 天）累计新建（平台租户 id=0 是种子数据不计）。"""
    week_from = today_from - timedelta(days=6)
    row = (
        await db.execute(
            select(
                func.count(Tenant.id).filter(Tenant.created_at >= today_from).label("today"),
                func.count(Tenant.id).filter(Tenant.created_at >= week_from).label("week"),
            ).where(Tenant.id != PLATFORM_TENANT_ID)
        )
    ).one()
    return {"value": int(row.today or 0), "week_total": int(row.week or 0)}


async def _online_model_replicas(db: AsyncSession) -> dict:
    """RUNNING 部署覆盖的模型数 + 活跃实例数与引擎分布（部署表 join 实例表）。"""
    online_models = (
        await db.execute(
            select(func.count(func.distinct(ModelDeployment.model_id))).where(
                ModelDeployment.status == DeploymentStatus.RUNNING
            )
        )
    ).scalar()
    engine_instance_count = {
        engine: int(n or 0)
        for engine, n in (
            await db.execute(
                select(ModelDeployment.engine, func.count(Instance.instance_id))
                .join(Instance, Instance.deployment_id == ModelDeployment.deployment_id)
                .where(Instance.status.in_(("RUNNING", "READY", "STARTING")))
                .group_by(ModelDeployment.engine)
            )
        ).all()
    }
    return {
        "online_models": int(online_models or 0),
        "replicas": sum(engine_instance_count.values()),
        "engine_mix": engine_instance_count,
    }


async def overview(db: AsyncSession) -> dict:
    """屏 1 整屏：零参数（窗口 = 今日本地零点 → 现在，对照 = 昨日零点 → 昨日同时刻）。"""
    now = now_local()
    today_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_from = today_from - timedelta(days=1)
    yesterday_to = now - timedelta(days=1)  # 昨日对照窗终点 = 昨日同时刻

    bucket_rows = await load_bucket_rows(db, yesterday_from, now)
    today_rows, yesterday_rows = split_dual_rows(bucket_rows, today_from, yesterday_to)

    today, yesterday = _window_sums(today_rows), _window_sums(yesterday_rows)
    active = await load_active_counts(db, yesterday_from, yesterday_to, today_from, now)
    today.update(active.get("today", {"models": 0, "tenants": 0}))
    yesterday.update(active.get("yesterday", {"models": 0, "tenants": 0}))

    kpi = overview_kpis(today, yesterday)
    kpi["new_tenants"] = await _new_tenants(db, today_from)
    trend = hourly_dual_trend(today_rows, yesterday_rows, today_from)

    panel = await screen_svc.service_panel(db)
    fleet = await _online_model_replicas(db)
    return {
        "ts": now.isoformat(),
        "window": {"from": today_from.isoformat(), "to": now.isoformat()},
        "kpi": kpi,
        "trend": trend,
        "upstream": await _upstream_overview(db, today_from, now),
        "service": {
            "online_models": fleet["online_models"],
            "replicas": fleet["replicas"],
            "engine_mix": fleet["engine_mix"],
            "deployments": panel["deployments"],
            "recent_deployments": panel["recent_deployments"],
            "nodes": {"online": panel["online_nodes"], "total": panel["total_nodes"]},
            "gpu": {"online": panel["gpu_online"], "total": panel["gpu_total"]},
            "clusters": panel["clusters"],
        },
    }
