"""实时大屏服务：热层/冷层读取 → 降采样 → KPI 环比 → 构成折叠 → 引擎网格。

一次调用组装整屏（/dashboard/realtime）：

- 窗口 ≤1h 优先读 scraper 热层（环形缓冲，粒度=抓取间隔）——5min 桶的最后一
  桶永远残缺，直接画曲线最右侧会持续跳动，这是热层存在的根本原因；
- scraper 未启动 / 热层无数据（进程重启冷启动）时自动回落冷层（metric_rollup
  5min 桶），响应 ``source`` 字段如实标注，前端不感知差异只提示粒度变化；
- 冷层在 SQL 侧按 (桶, 维度) 预聚合再进 Python——逐行拉原始桶行会在组合数
  ×桶数的规模下阻塞事件循环（该端点与管理端 REST/WS 同进程）；
- 趋势与构成共用同一张槽位网格（同下标同 ts），构成整窗一次性返回、光标擦洗
  在前端本地完成（逐帧请求会打爆后端）；构成折叠 top-5 + ``__other__``，
  颜色跟 key 走不跟排名走，由前端保证；
- KPI 环比对照窗由调用方给定（``prev_from`` / ``prev_to``；热层为此多保留
  一小时）——后端不再知道「今日」「近1h」这类语义，也就无从推断该跟哪段比；
  P95 按区间合并直方图桶后插值（单轮样本太少，插值不稳）；在途的瞬时
  值只有热层有——冷层置 null 而不是拿桶内峰值冒充（那是单 target 的 5min 峰值，
  量纲不同）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from llmpod.apps.webserver.metrics_scraper import (
    _BUCKET_SECONDS,
    _DIM_RING_RETENTION_SECONDS,
    MetricsScraper,
    get_scraper,
)
from llmpod.core.exceptions import LLMPodValidationError
from llmpod.models import Instance, MetricEngineRollup, MetricRollup, ModelDeployment
from llmpod.services.metric_query_service import (
    DASHBOARD_DIMS,
    LATENCY_BUCKET_BOUNDS,
    TRUNCATED_FINISH_REASONS,
    bucket_floor_expr,
    bucket_seconds_for,
    local_offset_seconds,
    resolve_compare_range,
    resolve_dashboard_range,
    truncation_rate,
    _as_local,
    _DIMENSION_COLUMNS,
    _merge_buckets,
    percentile_from_buckets,
)

from llmpod.utils.time import now_local

# 趋势/构成的服务端降采样上限（点数）；构成面板系列上限 top-5 + 其他
_MAX_TREND_POINTS = 120
_TOP_ITEMS = 5
_OTHER_KEY = "__other__"
# 原始 SQL 里用的截断原因字面量（值是 metric_query_service 的模块常量，无外部输入）
_TRUNCATED_FINISH_REASONS_SQL = ", ".join(f"'{reason}'" for reason in TRUNCATED_FINISH_REASONS)

# 大屏的粒度期望：**跨日就看日**（≤48h 仍按小时）。梯子本身在
# metric_query_service.pick_granularity，这条界线由调用方带进去——大屏要「跨日
# 就看日」，部署详情那条下钻曲线要 7 天内都给小时点，两边各说各的期望。
_DASHBOARD_HOUR_MAX_SPAN_SECONDS = 48 * 3600

# 走热层的最大回溯跨度（秒）：区间起点最多能往回这么远。取**构成维度环**的保留
# 深度而不是 __all__ 环的——后者是它的两倍（见 metrics_scraper._DIM_RING_RETENTION_SECONDS），
# 按 __all__ 判会让构成面板整块空掉而 KPI 满值的请求仍走热层。
_HOT_MAX_AGE_SECONDS = float(_DIM_RING_RETENTION_SECONDS)

# 闸门容差：前端按**自己的**钟算 from=now-3600，请求到服务端时服务端的 now 已经
# 往前走了（时钟偏差 + 传输 + 处理）。严格比较会让所有「近1小时」都判成过期、
# 一律回落冷层——热层等于白留。60s 足够吸收偏差，代价只是环尾最多 1 分钟的点
# 取不到（构成面板左边缘可能少一格，KPI 仍以 __all__ 环为准）。
_HOT_GATE_SLACK_SECONDS = 60.0


@dataclass(frozen=True)
class RealtimeFilter:
    """整屏的可选维度筛选（模型别名 / 租户 / API Key）；三个都 None 即不过滤。

    三列在 API 网关侧两张表上都有（``metric_rollup`` 与 ``request_log``），所以
    KPI / 趋势 / 构成 / 四张分布 / 截断率 / 吐字速度 能一起下推，不会出现
    「表格筛了、图没筛」的上下对不上。

    **引擎网格不参与筛选**：那是实例视角（容器在不在跑），同一部署可被多条
    路由以不同别名发布，别名到实例没有唯一映射；要看某个模型的实例，用
    ``dim=deployment`` 或去屏 3。
    """

    model: str | None = None
    tenant_id: int | None = None
    api_key_id: int | None = None

    @property
    def active(self) -> bool:
        # 与 _filter_parts 同一条规则：空串视为未过滤——否则
        # ``?model=`` 会在「不筛任何行」的同时把数据源翻到冷层
        return bool(self.model) or self.tenant_id is not None or self.api_key_id is not None


@dataclass(frozen=True)
class LogWindowStats:
    """``load_log_window_stats`` 的产物：request_log 落账行的窗口统计。

    两窗（当前/对照）各一组截断率与吐字速度——KPI 要环比；长度分布只取当前窗
    （分布面板不做环比），故只有一份。
    """

    trunc_cur: float | None
    trunc_prev: float | None
    tps_cur: float | None
    tps_prev: float | None
    prompt_len: dict | None
    generation_len: dict | None


def _filter_parts(filt: RealtimeFilter | None, alias: str) -> tuple[list, str, dict]:
    """同一组筛选的两种表达**一次产出**：ORM 条件 + 裸 SQL 片段（含绑定参数）。

    一起产出是为了不可能分叉——两套写法各写一遍时，加第 4 个维度只改一处就会
    让一半面板筛了、另一半没筛，且不报错。``alias`` 是裸 SQL 的表别名前缀
    （``"m."`` 或空串）；列名写死，值一律走绑定参数。
    """
    if filt is None:
        return [], "", {}
    orms: list = []
    clause, params = "", {}
    if filt.model:
        orms.append(MetricRollup.model_alias == filt.model)
        clause += f" AND {alias}model_alias = :f_model"
        params["f_model"] = filt.model
    if filt.tenant_id is not None:
        orms.append(MetricRollup.tenant_id == filt.tenant_id)
        clause += f" AND {alias}tenant_id = :f_tenant"
        params["f_tenant"] = filt.tenant_id
    if filt.api_key_id is not None:
        orms.append(MetricRollup.api_key_id == filt.api_key_id)
        clause += f" AND {alias}api_key_id = :f_apikey"
        params["f_apikey"] = filt.api_key_id
    return orms, clause, params


# ---- 槽位聚合（热层点与冷层聚合行统一收口到同一形状） ----


def _empty_slot(ts: datetime) -> dict:
    return {
        "ts": ts,
        "dur": 0.0,
        "requests": 0,
        "errors": 0,
        "rejected": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "latency_sum_ms": 0,
        "latency_count": 0,
        "ttft_sum_ms": 0,
        "ttft_count": 0,
        "in_flight_peak": 0,
        "in_flight_last": 0,
    }


def _slot_index(ts: datetime, from_: datetime, slot_seconds: float) -> int:
    return int((ts - from_).total_seconds() // slot_seconds)


def _slot_for(slots: dict, idx: int, from_: datetime, slot_seconds: float) -> dict:
    """取槽；命中时不构造临时 dict（读路径每个点都会走到这里）。"""
    slot = slots.get(idx)
    if slot is None:
        slot = _empty_slot(from_ + timedelta(seconds=idx * slot_seconds))
        slots[idx] = slot
    return slot


def slot_hot_points(points: list, from_: datetime, to: datetime, slot_seconds: float) -> list[dict]:
    """热层点序列 → 等宽槽位聚合（只含 [from, to) 内的点）。

    ``dur`` 取槽内各点 dur 之和（点的 dur 已按该 key 距上一点的跨度记，漏抓
    跨度与稀疏 key 的静默期都由点自身携带，速率按真实覆盖时长摊）。
    """
    slots: dict[int, dict] = {}
    for p in points:
        if not (from_ <= p.ts < to):
            continue
        slot = _slot_for(slots, _slot_index(p.ts, from_, slot_seconds), from_, slot_seconds)
        slot["dur"] += p.dur or 0.0
        slot["requests"] += p.requests
        slot["errors"] += p.errors
        slot["rejected"] += p.rejected
        slot["prompt_tokens"] += p.prompt_tokens
        slot["completion_tokens"] += p.completion_tokens
        slot["total_tokens"] += p.total_tokens
        slot["cached_tokens"] += p.cached_tokens
        slot["latency_sum_ms"] += p.latency_sum_ms
        slot["latency_count"] += p.latency_count
        slot["ttft_sum_ms"] += p.ttft_sum_ms
        slot["ttft_count"] += p.ttft_count
        slot["in_flight_peak"] = max(slot["in_flight_peak"], p.in_flight)
        slot["in_flight_last"] = p.in_flight
    return [slots[i] for i in sorted(slots)]


def slot_agg_rows(rows: list, from_: datetime, to: datetime, slot_seconds: float) -> list[dict]:
    """冷层聚合行（每 桶×维度key 一行）→ 槽位聚合。

    行内 errors/rejected 已由 SQL FILTER 算好；dur 按桶宽计（写侧攒桶网格的
    _BUCKET_SECONDS，两侧同宽有对账单测）——一行恰是一个桶一个 key，不会
    再按标签组合数放大。在途瞬时值冷层没有（in_flight_* 不动，恒 0 不出
    KPI——realtime 冷层路径整体置 null）。
    """
    slots: dict[int, dict] = {}
    # PG 的 sum(bigint) 返回 numeric，asyncpg 解码为 Decimal——与 float 分母
    # 相除抛 TypeError。聚合行入口处归一为 int（sum(integer) 本就是 int，一并
    # 处理保持行内类型一致）
    for r in rows:
        ts = r.bucket_start.astimezone()
        if not (from_ <= ts < to):
            continue
        slot = _slot_for(slots, _slot_index(ts, from_, slot_seconds), from_, slot_seconds)
        slot["dur"] += _BUCKET_SECONDS
        slot["requests"] += int(r.requests or 0)
        slot["errors"] += int(r.errors or 0)
        slot["rejected"] += int(r.rejected or 0)
        slot["prompt_tokens"] += int(r.prompt_tokens or 0)
        slot["completion_tokens"] += int(r.completion_tokens or 0)
        slot["total_tokens"] += int(r.total_tokens or 0)
        slot["cached_tokens"] += int(r.cached_tokens or 0)
        slot["latency_sum_ms"] += int(r.latency_sum_ms or 0)
        slot["latency_count"] += int(r.latency_count or 0)
        slot["ttft_sum_ms"] += int(r.ttft_sum_ms or 0)
        slot["ttft_count"] += int(r.ttft_count or 0)
    return [slots[i] for i in sorted(slots)]


def merge_agg_rows_by_bucket(rows: list) -> list:
    """(桶, key) 聚合行 → 按桶求和的全量行（跨维度 key 合并，NULL key 一并计入）。"""
    from types import SimpleNamespace

    by_bucket: dict[datetime, dict] = {}
    # 同 slot_agg_rows：sum(bigint) 的 Decimal 在入口归一为 int
    for r in rows:
        bucket = r.bucket_start.astimezone()
        acc = by_bucket.get(bucket)
        if acc is None:
            acc = {
                "requests": 0,
                "errors": 0,
                "rejected": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "latency_sum_ms": 0,
                "latency_count": 0,
                "ttft_sum_ms": 0,
                "ttft_count": 0,
            }
            by_bucket[bucket] = acc
        acc["requests"] += int(r.requests or 0)
        acc["errors"] += int(r.errors or 0)
        acc["rejected"] += int(r.rejected or 0)
        acc["prompt_tokens"] += int(r.prompt_tokens or 0)
        acc["completion_tokens"] += int(r.completion_tokens or 0)
        acc["total_tokens"] += int(r.total_tokens or 0)
        acc["cached_tokens"] += int(r.cached_tokens or 0)
        acc["latency_sum_ms"] += int(r.latency_sum_ms or 0)
        acc["latency_count"] += int(r.latency_count or 0)
        acc["ttft_sum_ms"] += int(r.ttft_sum_ms or 0)
        acc["ttft_count"] += int(r.ttft_count or 0)
    return [
        SimpleNamespace(bucket_start=bucket, dim_key=None, **fields) for bucket, fields in sorted(by_bucket.items())
    ]


def hot_slot_seconds(window_seconds: float, round_gap: float) -> float:
    """热层槽宽：按跨度与上限点数取整到抓取间隔的整数倍（不小于点距——
    点距比槽宽还宽时改用点距，空槽无意义）。"""
    slot = max(round_gap, math.ceil(window_seconds / _MAX_TREND_POINTS / round_gap) * round_gap)
    return float(slot)


def cold_slot_seconds(from_: datetime, to: datetime) -> float:
    """冷层槽宽 = 大屏梯子选出的桶宽（与热力图格宽同一个算式、同一条期望）。

    梯子的最坏档是 90 天 / 1d = 90 点，本就在 ``_MAX_TREND_POINTS`` 以内——档位
    自己保证了点数上界，不需要再在这里二次升档（曾经有一层 while 兜底，梯子改成
    按跨度选档后它永远跑不到，留着只会让人以为这里有第二道保护）。
    """
    return float(bucket_seconds_for(from_, to, hourly_span_limit_seconds=_DASHBOARD_HOUR_MAX_SPAN_SECONDS))


def _gran_label(seconds: float) -> str:
    """槽宽秒数 → 展示标签；整时/整日给 1h/1d 形态（与冷层词表同写法）。"""
    if seconds % 86400 == 0 and seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds)}s"


# ---- KPI 与派生量 ----


def _totals(items: list[dict]) -> dict:
    """槽位求和（KPI 的分子分母原料；dur 是覆盖时长不是墙钟跨度）。"""
    out = _empty_slot(now_local())
    out["ts"] = None
    for s in items:
        out["dur"] += s.get("dur") or 0.0
        for key in (
            "requests",
            "errors",
            "rejected",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
            "latency_sum_ms",
            "latency_count",
            "ttft_sum_ms",
            "ttft_count",
        ):
            out[key] += s.get(key) or 0
        out["in_flight_peak"] = max(out["in_flight_peak"], s.get("in_flight_peak") or 0)
        out["in_flight_last"] = s.get("in_flight_last") or 0
    return out


def _avg_ms(sum_ms: int, count: int) -> int | None:
    return round(sum_ms / count) if count else None


def _delta_pct(cur: float | None, prev: float | None) -> float | None:
    """环比百分比；两侧任一缺席都置 null。

    ``cur`` 也要守：当前窗无样本、对照窗有（如吐字速度在低峰时段）时
    ``cur`` 是 None，只守 prev 会在这里 TypeError。
    """
    if cur is None or prev is None or prev <= 0:
        return None
    return round((cur - prev) / prev * 100, 1)


def _delta_pp(cur: float | None, prev: float | None) -> float | None:
    """比率差的百分点（存的是 0-1 小数，展示按 pp ×100）。"""
    if cur is None or prev is None:
        return None
    return round((cur - prev) * 100, 2)


def _delta_ms(cur: int | None, prev: int | None) -> int | None:
    if cur is None or prev is None:
        return None
    return cur - prev


def _merge_hist_entries(entries: list, from_: datetime, to: datetime) -> tuple[list[int], list[int]]:
    """窗口内直方图逐轮增量合并 → (latency 桶, ttft 桶)。

    热层路径与冷层同源同语义：``entries`` 是 scraper 每轮的累计直方图快照
    （``(ts, lat, ttft)``），按各数组位相加即得整窗累计桶——复用冷层同款
    ``_merge_buckets``，缺位/短数组语义两侧一致。
    """
    lat = [0] * (len(LATENCY_BUCKET_BOUNDS) + 1)
    ttft = [0] * (len(LATENCY_BUCKET_BOUNDS) + 1)
    for ts, lat_row, ttft_row in entries:
        if from_ <= ts < to:
            _merge_buckets(lat, lat_row)
            _merge_buckets(ttft, ttft_row)
    return lat, ttft


def _p95_from_hist_entries(entries: list, from_: datetime, to: datetime) -> int | None:
    """窗口内直方图逐轮增量合并 → P95 插值（热层路径）。"""
    merged, _ttft = _merge_hist_entries(entries, from_, to)
    return percentile_from_buckets(merged, LATENCY_BUCKET_BOUNDS, 0.95)


def _rate_kpis(cur: dict, prev: dict, window_seconds: float, min_span: float = 0.0) -> dict:
    """窗口总量 → KPI 字段并集（速率/容量/健康三类卡共用一套分子分母）。

    速率分母 = 覆盖时长 dur。dur=0 但有量的窗口（热层重启后的首点、修剪后
    复现的 key）按单轮抓取间隔 min_span 估——首点 dur 记 0 是「跨度未知」，
    不是「整窗覆盖」，拿整窗当分母会把速率压成近停摆；dur=0 且无量则按整窗
    摊（结果为 0，与「窗口空」语义一致）。

    在途（in_flight）不在此产——瞬时值只有热层有，冷层拿桶内峰值冒充会让
    同一张卡在两个数据源之间跳变，由 realtime() 按数据源各自组装。
    """

    def _span(total: dict) -> float:
        if total["dur"] > 0:
            return total["dur"]
        if total["requests"] or total["total_tokens"]:
            return min_span or window_seconds
        return window_seconds

    cur_span = _span(cur)
    prev_span = _span(prev)
    cur_forwarded = cur["requests"] - cur["rejected"]
    prev_forwarded = prev["requests"] - prev["rejected"]

    cur_error_rate = round(cur["errors"] / cur_forwarded, 4) if cur_forwarded > 0 else 0.0
    prev_error_rate = round(prev["errors"] / prev_forwarded, 4) if prev_forwarded > 0 else None
    cur_hit = round(cur["cached_tokens"] / cur["prompt_tokens"], 4) if cur["prompt_tokens"] else None
    prev_hit = round(prev["cached_tokens"] / prev["prompt_tokens"], 4) if prev["prompt_tokens"] else None
    cur_lat = _avg_ms(cur["latency_sum_ms"], cur["latency_count"])
    prev_lat = _avg_ms(prev["latency_sum_ms"], prev["latency_count"])
    cur_ttft = _avg_ms(cur["ttft_sum_ms"], cur["ttft_count"])
    prev_ttft = _avg_ms(prev["ttft_sum_ms"], prev["ttft_count"])

    # prev 窗口无数据时速率为 0，_delta_pct 对 ≤0 返回 None（环比置空）——
    # 不需要单独的「有无数据」判断
    cur_qps = round(cur["requests"] / cur_span, 3)
    prev_qps = round(prev["requests"] / prev_span, 3)
    cur_tpm = round(cur["total_tokens"] / (cur_span / 60), 1)
    prev_tpm = round(prev["total_tokens"] / (prev_span / 60), 1)

    return {
        "qps": {"value": cur_qps, "delta_pct": _delta_pct(cur_qps, prev_qps)},
        "tpm": {"value": cur_tpm, "delta_pct": _delta_pct(cur_tpm, prev_tpm)},
        "error_rate": {"value": cur_error_rate, "delta_pp": _delta_pp(cur_error_rate, prev_error_rate)},
        "cache_hit_rate": {"value": cur_hit, "delta_pp": _delta_pp(cur_hit, prev_hit)},
        "latency_avg_ms": {"value": cur_lat, "delta_ms": _delta_ms(cur_lat, prev_lat)},
        "ttft_avg_ms": {"value": cur_ttft, "delta_ms": _delta_ms(cur_ttft, prev_ttft)},
    }


def engine_kpis(snapshots: list) -> dict:
    """引擎网格快照 → KV 水位（最忙实例）与排队总和（容量侧 KPI）。

    只统计 ok 的快照；无可用快照或字段未上报时值为 None（前端渲染 --，不显示
    0——「没抓到」与「真的是 0」是两回事，与引擎网格单实例卡同口径）。
    """

    def _num(v: float | None) -> int | None:
        # NaN/Inf 文本格式可出现，int() 会抛 → 过滤为 None
        if v is None or v != v or v in (float("inf"), float("-inf")):
            return None
        return int(v)

    with_kv = [s for s in snapshots if s.ok and s.kv_cache_util is not None]
    busiest = max(with_kv, key=lambda s: s.kv_cache_util) if with_kv else None
    # 排队总和只和「上报了排队数」的实例（混合集群里是已知部分的总和）；
    # 没有任何实例上报（未收录引擎/字段改名）时为 None，不冒充 0
    waiting_vals = [w for w in (_num(s.waiting_requests) for s in snapshots if s.ok) if w is not None]
    return {
        "kv_cache_util": {
            "value": round(busiest.kv_cache_util, 4) if busiest else None,
            "instance_id": busiest.instance_id if busiest else None,
        },
        "waiting_requests": {"value": sum(waiting_vals) if waiting_vals else None},
    }


# ---- 趋势与构成 ----


def _slot_tpm(slot: dict) -> float:
    """槽位 tpm；dur=0（覆盖时长未知）记 0——构成与总量共用此分母口径。"""
    span = slot.get("dur") or 0.0
    return round(slot["total_tokens"] / (span / 60), 1) if span > 0 else 0.0


def trend_payload(total_slots: list[dict]) -> tuple[dict, list[float]]:
    """总量槽位 → (趋势载荷, 逐槽 tpm)。索引基准取首槽值，≤0 的键省略。

    dur=0 的槽（首点跨度假未知）tpm 记 0 而非按槽宽摊——与构成项同分母，
    保证「构成之和 ≈ 总量」在重启后首槽也成立。点内 ``requests`` /
    ``latency_avg_ms`` 为槽内计数与均值（屏 2 多日堆叠图的计数轴）。
    """
    points = []
    tpm_series = []
    for s in total_slots:
        span = s["dur"] or 0.0
        tpm = _slot_tpm(s)
        points.append(
            {
                "ts": s["ts"].isoformat(),
                "qpm": round(s["requests"] / (span / 60), 1) if span > 0 else 0.0,
                "tpm": tpm,
                "prompt_tpm": round(s["prompt_tokens"] / (span / 60), 1) if span > 0 else 0.0,
                "completion_tpm": round(s["completion_tokens"] / (span / 60), 1) if span > 0 else 0.0,
                "requests": s["requests"],
                "latency_avg_ms": _avg_ms(s["latency_sum_ms"], s["latency_count"]),
            }
        )
        tpm_series.append(tpm)
    baseline = {}
    if points:
        if points[0]["qpm"] > 0:
            baseline["qpm"] = points[0]["qpm"]
        if points[0]["tpm"] > 0:
            baseline["tpm"] = points[0]["tpm"]
    return {"baseline": baseline, "points": points}, tpm_series


def key_slot_tpm(key_slots: dict[str, list[dict]], total_slots: list[dict]) -> list[dict[str, float]]:
    """每个网格槽 → 各 key 的 tpm（与总量槽同网格对齐；key 缺槽按 0 补）。

    分母与总量线一致：dur=0 的槽记 0，不按槽宽摊——两个分母会让单 key
    占比超 100%、堆叠构成与总量线对不上。
    """
    series: list[dict[str, float]] = [{} for _ in total_slots]
    for key, slots in key_slots.items():
        by_ts = {s["ts"]: s for s in slots}
        for i, grid in enumerate(total_slots):
            s = by_ts.get(grid["ts"])
            series[i][key] = _slot_tpm(s) if s else 0.0
    return series


def key_slot_requests(key_slots: dict[str, list[dict]], total_slots: list[dict]) -> list[dict[str, int]]:
    """每个网格槽 → 各 key 的请求计数（与 tpm 序列同网格同对齐，缺槽补 0）。

    计数不依赖 dur（无速率分母问题），是屏 2「按请求数堆叠」的主数据；
    前端拿整窗序列本地算连续涨/跌天数，服务端不开专门端点。
    """
    series: list[dict[str, int]] = [{} for _ in total_slots]
    for key, slots in key_slots.items():
        by_ts = {s["ts"]: s for s in slots}
        for i, grid in enumerate(total_slots):
            s = by_ts.get(grid["ts"])
            series[i][key] = s["requests"] if s else 0
    return series


def fold_composition(
    key_slots: dict[str, list[dict]],
    series: list[dict[str, float]],
    req_series: list[dict[str, int]],
    total_slots: list[dict],
    tpm_series: list[float],
) -> dict:
    """构成折叠：按窗口 token 总量取 top-5，其余并入 ``__other__``。

    ``__other__`` 可能已作为热层 key 上界折叠的产物存在——两次折叠语义一致
    （都是「长尾合并」），直接相加。``tpm`` 与 ``requests`` 双序列共用同一
    top-5 名单（按 token 排名），保证两幅堆叠图的成员一致。
    """
    totals = {key: sum(s["total_tokens"] for s in slots) for key, slots in key_slots.items() if key != _OTHER_KEY}
    ranked = sorted(totals, key=lambda k: totals[k], reverse=True)
    top = ranked[:_TOP_ITEMS]
    items = [
        {
            "key": key,
            "tpm": [{"ts": g["ts"].isoformat(), "value": series[i].get(key, 0.0)} for i, g in enumerate(total_slots)],
            "requests": [
                {"ts": g["ts"].isoformat(), "value": req_series[i].get(key, 0)} for i, g in enumerate(total_slots)
            ],
        }
        for key in top
    ]
    rest = ranked[_TOP_ITEMS:]
    if rest or _OTHER_KEY in key_slots:
        items.append(
            {
                "key": _OTHER_KEY,
                "tpm": [
                    {
                        "ts": g["ts"].isoformat(),
                        "value": round(
                            sum(series[i].get(k, 0.0) for k in rest) + series[i].get(_OTHER_KEY, 0.0),
                            1,
                        ),
                    }
                    for i, g in enumerate(total_slots)
                ],
                "requests": [
                    {
                        "ts": g["ts"].isoformat(),
                        "value": sum(req_series[i].get(k, 0) for k in rest) + req_series[i].get(_OTHER_KEY, 0),
                    }
                    for i, g in enumerate(total_slots)
                ],
            }
        )
    total = [{"ts": g["ts"].isoformat(), "tpm": tpm_series[i]} for i, g in enumerate(total_slots)]
    return {"total": total, "items": items}


# ---- 冷层 SQL 聚合（行数 = 桶数 × 维度成员数，不是桶数 × 标签组合数） ----


async def load_agg_rows(
    db: AsyncSession, from_: datetime, to: datetime, dim: str, filt: RealtimeFilter | None = None
) -> list:
    """窗口内桶行 → SQL 侧按 (桶起点, 维度列) 聚合；errors/rejected 由 FILTER 拆好。

    一行 = 一个 5min 桶 × 一个维度成员（含 NULL 维度组，跨 key 求和时计入
    总量、不进构成）。``filt`` 非空时只取匹配的行（KPI / 趋势 / 构成 一起收窄）。
    """
    col = _DIMENSION_COLUMNS[dim]
    m = MetricRollup
    rows = (
        await db.execute(
            select(
                m.bucket_start.label("bucket_start"),
                col.label("dim_key"),
                func.sum(m.requests).label("requests"),
                func.sum(m.requests).filter(m.status == "error").label("errors"),
                func.sum(m.requests).filter(m.status == "rejected").label("rejected"),
                func.sum(m.prompt_tokens).label("prompt_tokens"),
                func.sum(m.completion_tokens).label("completion_tokens"),
                func.sum(m.total_tokens).label("total_tokens"),
                func.sum(m.cached_tokens).label("cached_tokens"),
                func.sum(m.latency_sum_ms).label("latency_sum_ms"),
                func.sum(m.latency_count).label("latency_count"),
                func.sum(m.ttft_sum_ms).label("ttft_sum_ms"),
                func.sum(m.ttft_count).label("ttft_count"),
            )
            .where(m.bucket_start >= from_, m.bucket_start < to, *_filter_parts(filt, "m.")[0])
            .group_by(m.bucket_start, col)
            .order_by(m.bucket_start)
        )
    ).all()
    return list(rows)


async def load_merged_hist_pair(
    db: AsyncSession,
    prev_from: datetime,
    prev_to: datetime,
    from_: datetime,
    to: datetime,
    filt: RealtimeFilter | None = None,
) -> tuple[list[int] | None, list[int] | None]:
    """两窗（[from_,to) 与 [prev_from,prev_to)）的延迟直方图桶合并，一次查询取回。

    ``WITH ORDINALITY`` 的序号是 1-based，落 0-based 数组要减一——错了会把
    累计桶整体右移一格（分位数偏高约一个桶），越界的 +Inf 桶还被静默丢弃
    （总样本数失真）。宽度短于词表的旧行按存在的位求和。两窗同查一次而非
    各查一次：unnest 聚合行数 = 桶行×13，同端点两窗的环比查询没必要付两遍
    扫描（side 标记按 bucket_start 归属拆分，≤26 行结果）。

    两窗不总是相邻（``today`` 的对照窗是昨日同期，与当前窗之间隔着昨日剩余
    时段）：夹在 [prev_to, from_) 里的行两侧都不属于，cur_side 为 NULL，调用
    方跳过。
    """
    merged_cur = [0] * (len(LATENCY_BUCKET_BOUNDS) + 1)
    merged_prev = [0] * (len(LATENCY_BUCKET_BOUNDS) + 1)
    _, clause, extra = _filter_parts(filt, "m.")
    rows = (
        await db.execute(
            text(
                "SELECT CASE WHEN m.bucket_start >= :from_ts THEN TRUE "
                "WHEN m.bucket_start < :prev_to_ts THEN FALSE END AS cur_side, "
                "u.idx AS idx, SUM(u.cnt) AS total "
                "FROM metric_rollup m, "
                "LATERAL unnest(m.latency_buckets) WITH ORDINALITY AS u(cnt, idx) "
                "WHERE m.bucket_start >= :prev_from_ts AND m.bucket_start < :to_ts"
                f"{clause} "
                "GROUP BY cur_side, u.idx"
            ),
            {"prev_from_ts": prev_from, "prev_to_ts": prev_to, "from_ts": from_, "to_ts": to, **extra},
        )
    ).all()
    if not rows:
        return None, None
    for row in rows:
        if row.cur_side is None:
            continue  # 两窗之间的空档行
        idx = int(row.idx) - 1  # 1-based 序号 → 0-based 下标
        if not 0 <= idx < len(merged_cur):
            continue
        merged = merged_cur if row.cur_side else merged_prev
        merged[idx] += int(row.total or 0)
    return merged_cur, merged_prev


def _volume_kpis(cur: dict, prev: dict) -> dict:
    """窗口总量 KPI（计数/均值类，与速率卡分开组装）：请求数、token 总量、平均上下文。

    ``calls`` 口径 = 触达上游（requests − rejected），与 /summary /overview 的
    同名卡一致——拒绝风暴时两张屏的数字必须能对上。``avg_context`` =
    total_tokens / 触达请求数（每请求平均 token 用量），delta 为绝对差
    （token 量纲），无样本侧为 None。
    """
    cur_forwarded = cur["requests"] - cur["rejected"]
    prev_forwarded = prev["requests"] - prev["rejected"]
    cur_ctx = round(cur["total_tokens"] / cur_forwarded) if cur_forwarded else None
    prev_ctx = round(prev["total_tokens"] / prev_forwarded) if prev_forwarded else None
    return {
        "calls": {"value": cur_forwarded, "delta_pct": _delta_pct(cur_forwarded, prev_forwarded)},
        "total_tokens_kpi": {
            "value": cur["total_tokens"],
            "delta_pct": _delta_pct(cur["total_tokens"], prev["total_tokens"]),
        },
        "avg_context": {
            "value": cur_ctx,
            "delta": cur_ctx - prev_ctx if cur_ctx is not None and prev_ctx is not None else None,
        },
    }


def _p95_or_none(merged: list[int] | None) -> int | None:
    if merged is None:
        return None
    return percentile_from_buckets(merged, LATENCY_BUCKET_BOUNDS, 0.95)


# ---- 分布与截断率（冷层窗口扫；直方图按 cumulative 桶计数合并） ----

# 慢请求阈值：latency 取 2500ms 桶界、ttft 取 1000ms 桶界（都在桶边界词表内，
# slow_ratio = 超阈样本 / 总样本，无需插值）
_SLOW_LATENCY_BOUND = 2500
_SLOW_TTFT_BOUND = 1000


def hist_distribution(merged: list[int] | None, avg_ms: int | None, slow_bound_ms: int) -> dict | None:
    """cumulative 直方图 → 分布载荷（桶计数 / 桶界 / 均值 / 慢请求占比）。

    cumulative 语义：``buckets[i]`` = ≤ bounds[i] 的样本数，末位是 +Inf 桶
    （总样本）。样本全部落在阈值及以下时 slow_ratio 为 0.0；无样本返回 None
    （前端渲染空态而非全 0 直方图）。
    """
    if merged is None or merged[-1] <= 0:
        return None
    total = merged[-1]
    bound_idx = LATENCY_BUCKET_BOUNDS.index(slow_bound_ms)
    slow = total - merged[bound_idx]
    return {
        "buckets": merged,
        "bounds": [*LATENCY_BUCKET_BOUNDS, None],
        "avg_ms": avg_ms,
        "slow_ratio": round(slow / total, 4),
    }


async def load_window_histograms(
    db: AsyncSession, from_: datetime, to: datetime, filt: RealtimeFilter | None = None
) -> tuple[list[int] | None, list[int] | None]:
    """窗口内 latency / ttft 双直方图桶合并（一次扫描，UNION ALL 拆两个数组列）。

    与 load_merged_hist_pair 同款 unnest WITH ORDINALITY（1-based 序号减一对位，
    宽度短于词表的旧行按存在的位求和）；``which`` 标记来自哪个数组列。
    """
    lat = [0] * (len(LATENCY_BUCKET_BOUNDS) + 1)
    ttft = [0] * (len(LATENCY_BUCKET_BOUNDS) + 1)
    _, clause, extra = _filter_parts(filt, "m.")
    rows = (
        await db.execute(
            text(
                "SELECT u.which AS which, u.idx AS idx, SUM(u.cnt) AS total "
                "FROM metric_rollup m, "
                "LATERAL (SELECT 'latency' AS which, idx, cnt FROM unnest(m.latency_buckets) WITH ORDINALITY AS x(cnt, idx) "
                "UNION ALL SELECT 'ttft', idx, cnt FROM unnest(m.ttft_buckets) WITH ORDINALITY AS y(cnt, idx)) u "
                "WHERE m.bucket_start >= :from_ts AND m.bucket_start < :to_ts"
                f"{clause} "
                "GROUP BY u.which, u.idx"
            ),
            {"from_ts": from_, "to_ts": to, **extra},
        )
    ).all()
    if not rows:
        return None, None
    for row in rows:
        idx = int(row.idx) - 1  # 1-based 序号 → 0-based 下标
        if not 0 <= idx < len(lat):
            continue
        target = lat if row.which == "latency" else ttft
        target[idx] += int(row.total or 0)
    return lat, ttft


# 输入/输出长度的累计桶上界（token）。一套边界供两者共用：生成长度集中在
# 前几档、输入长度铺得更开，同一套正好覆盖常见 50~8k 并留到 32k 看长尾。
LEN_BUCKET_BOUNDS = (128, 512, 1024, 2048, 4096, 8192, 16384, 32768)


def _len_bucket_columns(column: str, prefix: str) -> str:
    """单维度长度的**两列**聚合片段：累计桶数组（jsonb）+ 均值。

    列名与 jsonb 都是字面量拼的（不接外部输入），别名由调用方显式给出——读回端
    就是 ``row.<prefix>_buckets`` / ``row.<prefix>_avg`` 两个属性，没有「键名要
    跟生成处对齐」的隐式契约。

    只统计**当前窗**且 token > 0 的行：对照窗那一侧没人读长度分布（分布不做
    环比），谓词写进 FILTER 里，对照窗的行就不必为这 20 个聚合付代价（实测省
    约两成）。token 为 0 的错误/中断行也不入桶——全堆进首桶会把分布形状带偏。
    """
    current = "created_at >= :from_ts"
    bucket = ", ".join(
        f"COUNT(*) FILTER (WHERE {current} AND {column} > 0 AND {column} <= {b})" for b in LEN_BUCKET_BOUNDS
    )
    total = f"COUNT(*) FILTER (WHERE {current} AND {column} > 0)"
    return (
        f"jsonb_build_array({bucket}, {total}) AS {prefix}_buckets, "
        f"AVG({column}) FILTER (WHERE {current} AND {column} > 0) AS {prefix}_avg"
    )


def _len_hist(buckets: list | None, avg) -> dict | None:
    """长度载荷 → 分布（累计桶 / 桶界 / 均值）；无样本（总量为 0）返回 None。

    末位是 +Inf 桶（有值的行数），为 0 即本窗没有带 token 的落账行——渲染空态
    而不是给一张全 0 直方图。
    """
    if not buckets or buckets[-1] <= 0:
        return None
    return {
        "buckets": buckets,
        "bounds": [*LEN_BUCKET_BOUNDS, None],
        "avg_tokens": round(float(avg)) if avg is not None else None,
    }


async def load_log_window_stats(
    db: AsyncSession,
    prev_from: datetime,
    prev_to: datetime,
    from_: datetime,
    to: datetime,
    filt: RealtimeFilter | None = None,
) -> LogWindowStats:
    """一次 request_log 扫描产出：截断率、输入/输出长度分布、吐字速度。

    这三样都只能从落账行拿（rollup 只有聚合值）：截断率要逐请求的
    ``finish_reason``、长度分布要逐请求的 token 数、吐字速度要逐请求的
    ``latency − ttft``。**合并成
    一条查询而不是各扫一遍**——该端点是 10s 轮询，扫描次数比聚合列数贵得多。

    ``WHERE`` 取两窗的**并集**而不是 [prev_from, to) 的连续区间：``today`` 的
    对照窗是昨日同期，两窗之间隔着昨日剩余时段，那些空档行两侧都不算——整段扫
    进来再逐行丢弃等于白付聚合代价（today 窗下空档约占四分之一行数）。

    分母是落了账的请求（拒绝不落 request_log），与 rollup 的 requests 口径不同。
    长度分布与均值只取当前窗（分布面板不做环比）；吐字速度两窗都要（KPI 有环比）。
    """
    _, log_clause, log_extra = _filter_parts(filt, "")
    aggregates = ", ".join(
        [
            # 截断率的分母只数**结束原因已知**的行（上游没给的、本列上线前的历史
            # 行不计入），否则未知会把率稀释；口径翻译见 TRUNCATED_FINISH_REASONS
            "COUNT(*) FILTER (WHERE finish_reason IS NOT NULL) AS finish_known",
            f"COUNT(*) FILTER (WHERE finish_reason IN ({_TRUNCATED_FINISH_REASONS_SQL})) AS limit_truncated",
            _len_bucket_columns("prompt_tokens", "prompt_len"),
            _len_bucket_columns("completion_tokens", "generation_len"),
            # 吐字速度的分子/分母：同一谓词，否则分母缺行时分子会把它的 token 算进来
            "SUM(completion_tokens) FILTER (WHERE ttft_ms IS NOT NULL AND latency_ms IS NOT NULL) AS gen_tokens",
            "SUM(latency_ms - ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL AND latency_ms IS NOT NULL) AS decode_ms",
        ]
    )
    rows = (
        await db.execute(
            text(
                "SELECT CASE WHEN created_at >= :from_ts THEN TRUE ELSE FALSE END AS cur_side, "
                f"{aggregates} "
                "FROM request_log "
                "WHERE (created_at >= :from_ts AND created_at < :to_ts) "
                "   OR (created_at >= :prev_from_ts AND created_at < :prev_to_ts)"
                f"{log_clause} "
                "GROUP BY cur_side"
            ),
            {"prev_from_ts": prev_from, "prev_to_ts": prev_to, "from_ts": from_, "to_ts": to, **log_extra},
        )
    ).all()

    cur_trunc = prev_trunc = None
    cur_tps = prev_tps = None
    prompt_len = generation_len = None
    for row in rows:
        trunc = truncation_rate(int(row.limit_truncated or 0), int(row.finish_known or 0))
        tps = _tokens_per_second(row.gen_tokens, row.decode_ms)
        if row.cur_side:
            cur_trunc, cur_tps = trunc, tps
            prompt_len = _len_hist(row.prompt_len_buckets, row.prompt_len_avg)
            generation_len = _len_hist(row.generation_len_buckets, row.generation_len_avg)
        else:
            prev_trunc, prev_tps = trunc, tps
    return LogWindowStats(
        trunc_cur=cur_trunc,
        trunc_prev=prev_trunc,
        tps_cur=cur_tps,
        tps_prev=prev_tps,
        prompt_len=prompt_len,
        generation_len=generation_len,
    )


def _tokens_per_second(gen_tokens, decode_ms) -> float | None:
    """吐字速度 = 输出 token / 生成耗时（毫秒）；无流式样本或耗时非正为 None。

    分母只含**有 ttft 的流式行**（非流式拿不到首帧时点，拿总延迟当生成耗时会把
    速度压成假的低值）。耗时 ≤0 是时钟粒度导致的退化，置空而不是给个除零爆值。
    """
    if gen_tokens is None or decode_ms is None:
        return None
    decode_ms = float(decode_ms)
    if decode_ms <= 0:
        return None
    return round(float(gen_tokens) / (decode_ms / 1000), 1)


# ---- 引擎网格 ----

_LIVE_INSTANCE_STATUSES = ("RUNNING", "READY", "STARTING", "CRASHED", "FAILED")


async def load_live_instances(db: AsyncSession, *, deployment_id: str | None = None) -> list[tuple]:
    """活跃实例清单（Instance 表 = 权威来源）：(instance_id, deployment_id, rank, status, endpoint, 部署名, engine)。

    engine_grid 与 engine_history 共用同一口径——两处各写一份 join 时，改状态
    词表只会改到一边，网格与热力图的实例名单就分叉了。endpoint 为 NULL 即
    headless worker（无 API 端点，也就没有 /metrics），网格据此标注「不暴露
    指标属预期」。
    """
    conds = [Instance.status.in_(_LIVE_INSTANCE_STATUSES)]
    if deployment_id is not None:
        conds.append(Instance.deployment_id == deployment_id)
    return list(
        (
            await db.execute(
                select(
                    Instance.instance_id,
                    Instance.deployment_id,
                    Instance.rank,
                    Instance.status,
                    Instance.endpoint,
                    ModelDeployment.name,
                    ModelDeployment.engine,
                )
                .join(ModelDeployment, ModelDeployment.deployment_id == Instance.deployment_id)
                .where(*conds)
            )
        ).all()
    )


async def engine_history(db: AsyncSession, *, from_: datetime | None = None, to: datetime | None = None) -> dict:
    """KV 热力图数据：活跃实例 × 等宽桶的 kv_avg/kv_max/抢占/抓到次数序列。

    单元格宽度随区间跨度走（``bucket_seconds_for``）：metric_engine_rollup 落库
    只有 5min 一行，宽区间按整倍数合并行——否则 7d 铺 2016 列、90d 上万列。
    合并按加权口径：均值以抓取次数加权、峰值取最大、``samples`` 求和（它同时
    是权重的来源）。

    桶行来自 metric_engine_rollup，``samples=0`` 的桶不存在行——整格无数据的
    空档不产生分组行，前端渲染空档而非伪造 0（「没抓到」不等于「水位为 0」）。
    非法区间抛 ``LLMPodValidationError``（webserver 统一映射 422）。
    """
    now = now_local()
    from_, to = resolve_dashboard_range(from_, to, now)
    bucket_seconds = bucket_seconds_for(from_, to, hourly_span_limit_seconds=_DASHBOARD_HOUR_MAX_SPAN_SECONDS)
    granularity = _gran_label(float(bucket_seconds))

    inst_rows = await load_live_instances(db)
    if not inst_rows:
        return {
            "window": {"from": from_.isoformat(), "to": to.isoformat(), "granularity": granularity},
            "instances": [],
        }
    # 桶起点按**本地**边界取整：日桶不加时区偏移会切在 UTC 零点（本地早上八点），
    # 5m/1h 桶在整小时时区下加不加都一样。偏移量取**服务端**本地时区而不是请求
    # 时间戳自带的 utcoffset——调用方可能用 …Z 编码传时间戳，那样日界会退回 UTC。
    tz_off = local_offset_seconds()
    bucket_index = cast(
        bucket_floor_expr(MetricEngineRollup.bucket_start, bucket_seconds, tz_off),
        BigInteger,
    )
    agg_rows = list(
        (
            await db.execute(
                select(
                    MetricEngineRollup.instance_id,
                    bucket_index.label("bucket_index"),
                    func.sum(MetricEngineRollup.samples).label("samples"),
                    func.sum(MetricEngineRollup.preemptions).label("preemptions"),
                    func.max(MetricEngineRollup.kv_cache_util_max).label("kv_max"),
                    # 加权分子与分母（权重 = samples）：除法留在 Python 侧——SQL 内做
                    # 除法在仓内 stub session 的单测里测不到，且 asyncpg 的
                    # Decimal/float 混算在本模块有过 500 前科
                    #
                    # 分母必须**只数有水位读数的行**：samples 计的是「成功抓取次数」，
                    # 引擎不暴露 kv_cache_util 的那些轮次同样计入（见 scraper 的
                    # acc.samples += 1 与 add_gauge 分开累加）。整列当分母会把
                    # 「抓到但没这个 gauge」的轮次算成水位读数为 0，格均值被系统性
                    # 拉低——5min 档一格一行时看不出来，粗格才会显形。
                    func.sum(MetricEngineRollup.kv_cache_util_avg * MetricEngineRollup.samples).label("kv_avg_sum"),
                    func.sum(MetricEngineRollup.samples)
                    .filter(MetricEngineRollup.kv_cache_util_avg.isnot(None))
                    .label("kv_avg_weight"),
                )
                .where(
                    MetricEngineRollup.bucket_start >= from_,
                    MetricEngineRollup.bucket_start < to,
                    MetricEngineRollup.instance_id.in_([r[0] for r in inst_rows]),
                )
                .group_by(MetricEngineRollup.instance_id, bucket_index)
                .order_by(bucket_index)
            )
        ).all()
    )
    by_instance: dict[str, list[dict]] = {}
    for r in agg_rows:
        samples = int(r.samples or 0)
        weight = int(r.kv_avg_weight or 0)
        kv_avg_sum = r.kv_avg_sum
        # 整格没采到水位（引擎不暴露该 gauge）时给 null，不给 0
        kv_avg = round(kv_avg_sum / weight, 4) if weight > 0 and kv_avg_sum is not None else None
        # 桶序号 → 起点：先还原时区偏移再还原整数序号，与落库行同为 UTC-aware
        ts = datetime.fromtimestamp(int(r.bucket_index) * bucket_seconds - tz_off, tz=timezone.utc)
        by_instance.setdefault(r.instance_id, []).append(
            {
                "ts": ts.isoformat(),
                "kv_avg": kv_avg,
                "kv_max": round(r.kv_max, 4) if r.kv_max is not None else None,
                "preemptions": int(r.preemptions or 0),
                # samples 是格内抓取次数之和，故它同时是上面均值的权重
                "samples": samples,
            }
        )
    instances = [
        {
            "instance_id": iid,
            "deployment_id": dep_id,
            "deployment_name": name,
            "engine": engine,
            "rank": rank,
            "status": status,
            "series": by_instance.get(iid, []),
        }
        for iid, dep_id, rank, status, _endpoint, name, engine in inst_rows
    ]
    return {
        "window": {"from": from_.isoformat(), "to": to.isoformat(), "granularity": granularity},
        "instances": instances,
    }


async def engine_grid(
    db: AsyncSession,
    scraper: MetricsScraper | None,
    *,
    deployment_id: str | None = None,
) -> list[dict]:
    """全部（或单部署）活跃实例 + scraper 快照实时值 + 数据年龄。

    实例清单与 rank/status 来自 Instance 表（权威来源）；实时值与 ok 来自 scraper
    内存快照（同进程直读）；从未抓到的实例 ok=False、指标全空（前端渲染
    「暂无数据」，不显示 0）。
    """
    rows = await load_live_instances(db, deployment_id=deployment_id)
    snapshots = {}
    if scraper is not None:
        if deployment_id is not None:
            snapshots = {s.instance_id: s for s in scraper.engine_snapshots_by_deployment(deployment_id)}
        else:
            snapshots = {s.instance_id: s for s in scraper.all_engine_snapshots()}
    now = now_local()
    items = []
    for iid, dep_id, rank, status, endpoint, dep_name, engine in rows:
        s = snapshots.get(iid)
        items.append(
            {
                "instance_id": iid,
                "deployment_id": dep_id,
                "deployment_name": dep_name,
                "engine": engine,
                "rank": rank,
                "status": status,
                "ok": bool(s and s.ok),
                # headless worker（无 API 端点）不暴露 /metrics 是结构使然，不是抓取故障
                "metrics_expected": endpoint is not None,
                "kv_cache_util": s.kv_cache_util if s else None,
                "running_requests": s.running_requests if s else None,
                "waiting_requests": s.waiting_requests if s else None,
                "prefix_cache_hit_rate": s.prefix_cache_hit_rate if s else None,
                "num_preemptions": s.num_preemptions if s else None,
                "age_ms": round((now - s.fetched_at).total_seconds() * 1000) if s and s.fetched_at else None,
            }
        )
    return items


# ---- 整屏组装 ----


async def realtime(
    db: AsyncSession,
    *,
    from_: datetime | None = None,
    to: datetime | None = None,
    prev_from: datetime | None = None,
    prev_to: datetime | None = None,
    dim: str = "model",
    model: str | None = None,
    tenant_id: int | None = None,
    api_key_id: int | None = None,
) -> dict:
    """一次返回整屏（KPI + 趋势 + 构成 + 引擎网格 + 分布）。

    非法 ``dim`` / 区间抛 ``LLMPodValidationError``（webserver 统一映射 422）——静默
    改写会让拼错参数的调用方看到「正常数据」，排障时无从发现。

    **环比对照窗由调用方给出**（``prev_from`` / ``prev_to``）：后端不再知道
    「今日」「近1h」这类语义，也就无从推断该跟哪一段比。前端按预设算区间时
    顺带算对照窗即可（今日 → 昨日同钟点；滚动窗 → 往前推一个跨度；多日窗不取
    对照）。不传 → 所有 ``delta_*`` 为 null，前端渲染 ``--``。

    ``model`` / ``tenant_id`` / ``api_key_id`` 是整屏筛选，作用于所有 API 网关侧
    面板（KPI / 趋势 / 构成 / 分布 / 截断率 / 吐字速度）；**引擎网格不筛**
    （见 ``RealtimeFilter``）。带了筛选一律走冷层：热层是**按维度分环**存的，
    跨维度筛选取不到（如「模型=A 下按 API Key 拆」），统一走冷层语义才一致
    ——响应里 ``source=cold`` 会如实标注。
    """
    now = now_local()
    # 钳位前的 to：客户端钟快于服务端时 to 会被钳回 now，钳位量要一并交给对照窗，
    # 否则等长校验会把「前后端钟差」报成「调用方给的区间非法」
    requested_to = _as_local(to)
    from_, to = resolve_dashboard_range(from_, to, now)
    to_shift_seconds = (requested_to - to).total_seconds() if requested_to is not None else 0.0
    prev_from, prev_to = resolve_compare_range(prev_from, prev_to, from_, to, to_shift_seconds=to_shift_seconds)
    window_seconds = (to - from_).total_seconds()
    if dim not in DASHBOARD_DIMS:
        raise LLMPodValidationError(f"非法维度：{dim}（可选 {', '.join(DASHBOARD_DIMS)}）")
    filt = RealtimeFilter(model=model, tenant_id=tenant_id, api_key_id=api_key_id)

    scraper = get_scraper()
    # 热层是内存环形缓冲，只覆盖最近一段。判定看**区间起点往回多远**：to 已被钳到
    # now，所以这一个条件同时限住了跨度与「落在多远的过去」。
    #
    # 深度取**构成维度环**的保留期而不是 __all__ 环的：后者是它的两倍，只按 __all__
    # 判会让「区间尾部落在维度环之外」的请求仍走热层——KPI/趋势满值，构成面板却
    # 全空，而响应看起来完全正常（filter 只引用 __all__ 与直方图）。改动前热层只
    # 服务锚在 now 的 1h 窗，这种形态不可达；区间化之后可达了。
    hot = scraper is not None and (now - from_).total_seconds() <= (_HOT_MAX_AGE_SECONDS + _HOT_GATE_SLACK_SECONDS)
    if hot and filt.active:
        # 带筛选一律走冷层：热层按维度分环存，跨维度筛选取不到（详见 RealtimeFilter）
        hot = False
    if hot:
        all_points = scraper.hot_series("__all__").get("", [])
        cur = [p for p in all_points if from_ <= p.ts < to]
        # 覆盖判定看区间内点的跨度而非「区间内存在点」：重启后第一个点（≤5s）
        # 就命中 liveness，区间只剩几秒数据、趋势近乎空白——冷层里上一个
        # 完整 5min 桶反而有数据。覆盖攒到 ≥ 半跨才接管（数据年龄由 age_ms
        # 如实标注，新鲜度不在此判定里）。区间整个落在缓冲之外时 cur 为空、
        # covered=0，同样在这里回落冷层。
        covered = (cur[-1].ts - cur[0].ts).total_seconds() if cur else 0.0
        if covered < window_seconds / 2:
            hot = False

    if hot:
        round_gap = float(scraper.proxy_interval)  # 槽宽不小于点距
        slot_seconds = hot_slot_seconds(window_seconds, round_gap)
        total_slots = slot_hot_points(cur, from_, to, slot_seconds)
        if prev_from is None:
            prev_slots = []
        else:
            prev = [p for p in all_points if prev_from <= p.ts < prev_to]
            prev_slots = slot_hot_points(prev, prev_from, prev_to, slot_seconds)
        cur_total = _totals(total_slots)
        prev_total = _totals(prev_slots)
        kpi = _rate_kpis(cur_total, prev_total, window_seconds, min_span=round_gap)
        # 在途瞬时值与窗口峰值只有热层有（跨 target 求和后的瞬时 gauge）
        kpi["in_flight"] = {"value": cur_total["in_flight_last"], "peak": cur_total["in_flight_peak"]}
        hist_entries = scraper.hot_histograms()
        cur_p95 = _p95_from_hist_entries(hist_entries, from_, to)
        # 未给对照窗时不取上一窗的 P95（None 传进区间比较会直接 TypeError）
        prev_p95 = _p95_from_hist_entries(hist_entries, prev_from, prev_to) if prev_from is not None else None
        kpi["p95_ms"] = {"value": cur_p95, "delta_ms": _delta_ms(cur_p95, prev_p95)}
        # 分布与 P95 同源（同一份逐轮快照）。热层唯一的窗口就是 1h——不在这里
        # 产，屏 2 最常用窗口的分布面板就恒为空态，热/冷回落切换时还会忽隐忽现
        lat_hist, ttft_hist = _merge_hist_entries(hist_entries, from_, to)
        key_slots = {
            key: slot_hot_points(points, from_, to, slot_seconds) for key, points in scraper.hot_series(dim).items()
        }
        source = "hot"
        gran_label = _gran_label(slot_seconds)
    else:
        slot_seconds = cold_slot_seconds(from_, to)
        # 未给对照窗时用当前窗左界占位：聚合行区间退化成 [from_, to)，切分后
        # prev_rows 自然为空、各 delta 置 null
        agg_rows = await load_agg_rows(db, prev_from or from_, to, dim, filt)
        cur_rows = [r for r in agg_rows if from_ <= r.bucket_start.astimezone() < to]
        prev_rows = [
            r for r in agg_rows if prev_from is not None and prev_from <= r.bucket_start.astimezone() < prev_to
        ]
        total_slots = slot_agg_rows(merge_agg_rows_by_bucket(cur_rows), from_, to, slot_seconds)
        prev_slots = (
            slot_agg_rows(merge_agg_rows_by_bucket(prev_rows), prev_from, prev_to, slot_seconds)
            if prev_from is not None
            else []
        )
        cur_total = _totals(total_slots)
        prev_total = _totals(prev_slots)
        kpi = _rate_kpis(cur_total, prev_total, window_seconds)
        lat_hist, ttft_hist = await load_window_histograms(db, from_, to, filt)
        if prev_from is not None:
            cur_hist, prev_hist = await load_merged_hist_pair(db, prev_from, prev_to, from_, to, filt)
        else:
            # 未给对照窗时当前直方图 = 上面那次扫描的 latency 输出，不重复扫
            cur_hist, prev_hist = None, None
        cur_merged = cur_hist if cur_hist is not None else lat_hist
        cur_p95 = _p95_or_none(cur_merged)
        prev_p95 = _p95_or_none(prev_hist)
        kpi["p95_ms"] = {"value": cur_p95, "delta_ms": _delta_ms(cur_p95, prev_p95)}
        # 在途瞬时值与窗口峰值只有热层有；冷层桶内峰值是单 target 的 5min
        # 峰值，量纲不同（热层是跨 target 求和后的瞬时），拿来冒充只会让同一
        # 张卡在两个数据源之间跳变——value/peak 一并置 null
        kpi["in_flight"] = {"value": None, "peak": None}
        grouped: dict[str, list] = {}
        for r in cur_rows:
            key = r.dim_key
            if key is None or key == "":
                continue  # 无维度身份（选路前拒绝等）不进构成，总量已含
            grouped.setdefault(str(key), []).append(r)
        key_slots = {key: slot_agg_rows(rws, from_, to, slot_seconds) for key, rws in grouped.items()}
        source = "cold"
        gran_label = _gran_label(slot_seconds)

    # request_log 落账行的一次扫描（与热/冷无关：落账是代理直写，不进桶）：
    # 截断率、吐字速度两窗都要（KPI 有环比），长度分布只取当前区间（分布不环比）
    log_stats = await load_log_window_stats(db, prev_from or from_, prev_to or from_, from_, to, filt)
    kpi["truncation_rate"] = {
        "value": log_stats.trunc_cur,
        "delta_pp": _delta_pp(log_stats.trunc_cur, log_stats.trunc_prev),
    }
    kpi["output_tps"] = {"value": log_stats.tps_cur, "delta_pct": _delta_pct(log_stats.tps_cur, log_stats.tps_prev)}

    # 四张分布一次组装（两分支各自赋好 lat_hist/ttft_hist 后到这里收口）
    distribution = {
        "latency": hist_distribution(lat_hist, kpi["latency_avg_ms"]["value"], _SLOW_LATENCY_BOUND),
        "ttft": hist_distribution(ttft_hist, kpi["ttft_avg_ms"]["value"], _SLOW_TTFT_BOUND),
        "prompt_len": log_stats.prompt_len,
        "generation_len": log_stats.generation_len,
    }

    kpi.update(_volume_kpis(cur_total, prev_total))

    trend, tpm_series = trend_payload(total_slots)
    series = key_slot_tpm(key_slots, total_slots)
    req_series = key_slot_requests(key_slots, total_slots)
    composition = fold_composition(key_slots, series, req_series, total_slots, tpm_series)

    kpi.update(engine_kpis(scraper.all_engine_snapshots() if scraper else []))
    engines = await engine_grid(db, scraper)
    # age_ms 只在热层路径有意义（热层点龄）；冷层的数据年龄由桶网格天然决定
    age_ms = None
    if source == "hot" and scraper is not None and scraper.last_round_at is not None:
        age_ms = round((now - scraper.last_round_at).total_seconds() * 1000)

    return {
        "ts": now.isoformat(),
        "window": {
            "from": from_.isoformat(),
            "to": to.isoformat(),
            "granularity": gran_label,
            "source": source,
            "age_ms": age_ms,
        },
        "kpi": kpi,
        "trend": trend,
        "composition": {**composition, "dim": dim},
        "distribution": distribution,
        "engines": engines,
    }
