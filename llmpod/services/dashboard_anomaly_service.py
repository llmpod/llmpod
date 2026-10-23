"""异常面板服务：拒绝归因（rollup）+ 错误粗分类（request_log）+ 上游健康。

屏 3「异常信息」面板一次取齐三块：

- 拒绝侧走 ``metric_rollup``（status=rejected 按 reject_reason 聚合，同一查询
  顺带取回窗口总请求——ratio 分母不加扫第二遍）——拒绝不落 request_log，
  rollup 是唯一来源，且无 request_log 行扫的索引依赖；
- 错误侧扫 ``request_log``（status='error'）按 ``error_message`` 关键词粗分类
  （SQL CASE，窗口内行数有限，分类表在模块常量里表驱动，新增类别改一处）；
- 上游健康复用 metric_query_service 的 upstream 聚合行（local/external +
  外部端点明细），本模块只做比率换算。

``ratio`` 的分母是 rollup 同窗 requests（两数据源并读：分子来自 request_log、
分母来自 rollup，口径注释见 anomaly()）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.models import MetricRollup
from llmpod.services import metric_query_service as mq
from llmpod.services.metric_query_service import (
    resolve_dashboard_range,
    upstream_success_rate,
)
from llmpod.utils.time import now_local

# error_message 粗分类表：关键词（小写包含匹配）→ kind，顺序即优先级，
# 先专后泛，兜底 other。产出侧词表（inference_handler._upstream_error_message /
# 流式 except 分支）只有三种形态：``upstream HTTP {status}``、
# ``upstream {ExceptionName}``、``stream failed: {ExceptionName}``——状态行
# 按前缀归类（upstream http 5* / 4*），异常类型名按关键词归类（ReadTimeout、
# ConnectError、RemoteProtocolError…）。SQL 侧是 LIKE：关键词里的 `_` 在 SQL
# 中是单字符通配（如 context_length 会匹配 contextXlength），Python 镜像是
# 子串精确匹配——语义差异只影响那一个下划线关键词，可忽略。
ERROR_KIND_MATCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("upstream_timeout", ("timeout", "timed out", "超时")),
    ("upstream_5xx", ("upstream http 5", "upstream 5xx", "502", "503", "504", "bad gateway", "service unavailable")),
    ("rate_limited", ("429", "rate limit", "限流", "限速")),
    ("upstream_4xx", ("upstream http 4", "401", "403", "404", "unauthorized", "鉴权", "认证失败")),
    ("upstream_connect", ("connect", "connection refused", "connection reset", "无法连接")),
    ("context_length", ("context length", "context_length", "上下文长度")),
    ("quota_exhausted", ("quota", "insufficient", "余额", "配额")),
)
_ERROR_KIND_OTHER = "other"


def _case_sql() -> str:
    """分类表 → SQL CASE 表达式（逐条 WHEN lower(error_message) LIKE ... THEN kind）。

    生成静态 SQL 片段（无用户输入拼接），参数化只用于时间窗。
    """
    whens = []
    for kind, keywords in ERROR_KIND_MATCHERS:
        pats = " OR ".join(f"lower(m.error_message) LIKE '%{kw}%'" for kw in keywords)
        whens.append(f"WHEN {pats} THEN '{kind}'")
    return "CASE " + " ".join(whens) + f" ELSE '{_ERROR_KIND_OTHER}' END"


# CASE 表达式是分类表的纯函数，模块加载时构建一次（每请求重建没有意义，
# 也让 asyncpg 的预编译语句缓存键保持稳定）
_ERROR_CASE_SQL = _case_sql()


def classify_error_message(message: str | None) -> str:
    """Python 侧同款分类（测试对账 / 行级调试用；与 SQL CASE 同一匹配表）。"""
    if not message:
        return _ERROR_KIND_OTHER
    lowered = message.lower()
    for kind, keywords in ERROR_KIND_MATCHERS:
        if any(kw in lowered for kw in keywords):
            return kind
    return _ERROR_KIND_OTHER


def _share(count: int, total: int) -> float:
    return round(count / total, 4) if total > 0 else 0.0


async def _reject_items(db: AsyncSession, from_: datetime, to: datetime) -> tuple[list[dict], int]:
    """rollup status=rejected 按 reject_reason 聚合 + 窗口总请求数（一条查询）。

    拒绝不落 request_log，rollup 是唯一来源。总请求不再单查一遍：全部请求按
    reject_reason 分组求和后加回来即全窗总和（无 rejected 行的窗内，总请求由
    调用方在 errors 侧归并时补齐——见 anomaly() 的 all_requests 合计）。
    """
    m = MetricRollup
    rows = (
        await db.execute(
            select(
                m.reject_reason.label("reason"),
                func.sum(m.requests).filter(m.status == "rejected").label("rejected"),
                func.sum(m.requests).label("all"),
            )
            .where(m.bucket_start >= from_, m.bucket_start < to)
            .group_by(m.reject_reason)
        )
    ).all()
    items = [
        {"kind": r.reason or "unknown", "count": int(r.rejected or 0), "side": "rejected"}
        for r in rows
        if int(r.rejected or 0) > 0
    ]
    items.sort(key=lambda x: x["count"], reverse=True)
    total_requests = sum(int(r.all or 0) for r in rows)
    return items, total_requests


async def _error_items(db: AsyncSession, from_: datetime, to: datetime) -> list[dict]:
    """request_log status='error' 按 error_message 粗分类（SQL CASE 聚合）。"""
    sql = text(
        f"SELECT kind, COUNT(*) AS count FROM ("
        f"SELECT {_ERROR_CASE_SQL} AS kind FROM request_log m "
        f"WHERE m.status = 'error' AND m.created_at >= :from_ts AND m.created_at < :to_ts"
        f") t GROUP BY kind"
    )
    rows = (await db.execute(sql, {"from_ts": from_, "to_ts": to})).all()
    items = [{"kind": r.kind, "count": int(r.count or 0), "side": "error"} for r in rows]
    items.sort(key=lambda x: x["count"], reverse=True)
    return items


def _upstream_block(type_rows: list, endpoint_rows: list, minutes: float) -> dict:
    """上游聚合行 → local/external 汇总卡 + 外部端点列表（success_rate/qpm/延迟）。"""

    def _card(rows: list) -> dict | None:
        if not rows:
            return None
        requests = sum(int(r.requests or 0) for r in rows)
        lat_sum = sum(int(r.latency_sum_ms or 0) for r in rows)
        lat_count = sum(int(r.latency_count or 0) for r in rows)
        return {
            "qpm": round(requests / minutes, 1),
            "success_rate": upstream_success_rate(
                requests, sum(int(r.errors or 0) for r in rows), sum(int(r.rejected or 0) for r in rows)
            ),
            "latency_avg_ms": round(lat_sum / lat_count) if lat_count else None,
        }

    local = _card([r for r in type_rows if r.upstream_type == "local"])
    external = _card([r for r in type_rows if r.upstream_type == "external"])
    providers = []
    for r in sorted(endpoint_rows, key=lambda x: int(x.requests or 0), reverse=True):
        requests = int(r.requests or 0)
        providers.append(
            {
                "name": r.endpoint_name,
                "qpm": round(requests / minutes, 1),
                "success_rate": upstream_success_rate(requests, int(r.errors or 0), int(r.rejected or 0)),
                "latency_avg_ms": round(int(r.latency_sum_ms or 0) / int(r.latency_count or 0))
                if int(r.latency_count or 0)
                else None,
            }
        )
    return {"local": local, "external": external, "providers": providers}


async def anomaly(db: AsyncSession, *, from_: datetime | None = None, to: datetime | None = None) -> dict:
    """异常面板整块：拒绝 + 错误 + 总量占比 + 上游健康。

    ``ratio`` 分母 = rollup 同区间全部 requests（含拒绝与未选路）；分子 =
    拒绝侧计数（rollup）+ 错误侧计数（request_log）。两个数据源的口径差
    （request_log 不落拒绝、rollup 不落 body 细节）在此声明：这是「异常
    占到达请求的比例」的工程近似，不是严格单源比率。
    """
    now = now_local()
    from_, to = resolve_dashboard_range(from_, to, now)
    # QPM 分母就是区间跨度——两个时间戳直接相减，不再需要「窗口名 → 秒数」的查表
    window_seconds = (to - from_).total_seconds()

    rejects, total_requests = await _reject_items(db, from_, to)
    errors = await _error_items(db, from_, to)
    # share 的分母取两侧合计（拒绝 share 占总异常、错误 share 占总异常——面板
    # 是一张「异常构成」图，不是两张独立的帕累托图）
    anomaly_total = sum(i["count"] for i in rejects) + sum(i["count"] for i in errors)
    for i in [*rejects, *errors]:
        i["share"] = _share(i["count"], anomaly_total)

    type_rows = await mq.upstream_type_rows(db, from_, to)
    # 本地-only 部署的常态：没有 external 流量就不必再扫一遍端点 join
    has_external = any(r.upstream_type == "external" for r in type_rows)
    endpoint_rows = await mq.upstream_endpoint_rows(db, from_, to) if has_external else []
    upstream = _upstream_block(type_rows, endpoint_rows, window_seconds / 60)

    return {
        "ts": now.isoformat(),
        "window": {"from": from_.isoformat(), "to": to.isoformat()},
        "anomaly": {
            "total": anomaly_total,
            "ratio": round(anomaly_total / total_requests, 4) if total_requests > 0 else 0.0,
            "items": [*rejects, *errors],
        },
        "upstream": upstream,
    }
