"""指标链路本地基准：ingest 吞吐 / 热层内存 / realtime 聚合 / 引擎大文本解析。

不带任何服务跑（无 PG / 无 HTTP），直接构造 Prometheus 文本喂 scraper 的
ingest 路径。用于回答两类问题：

- 抓取成本随 label 组合数的扩展曲线（决定 proxy 快档 5s 在多大规模下安全）；
- 热层内存上界（key 上界 × 环形深度的最坏形态）与 realtime 单请求聚合耗时。

用法：``uv run python scripts/bench_metrics_pipeline.py``
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta, timezone

from prometheus_client.parser import text_string_to_metric_families
from tracemalloc import get_traced_memory, start as tm_start, stop as tm_stop

from llmpod.apps.webserver.metrics_scraper import MetricsScraper, _HotPoint
from llmpod.config.settings import Settings
from llmpod.services.dashboard_realtime_service import (
    fold_composition,
    key_slot_requests,
    key_slot_tpm,
    slot_hot_points,
    trend_payload,
)
from llmpod.services.metric_query_service import LATENCY_BUCKET_BOUNDS

# 直方图边界跟随打点端词表：scraper._bucket_index 按值匹配边界，打点端改了
# 边界后硬编码副本的样本会被静默丢弃，基准就量不出真实 ingest 成本
BUCKETS = LATENCY_BUCKET_BOUNDS
TZ = timezone.utc


def build_text(combos, base):
    """每个 label 组合 37 个样本（counter×7 + 两直方图×15 + ...），贴近真实形状。"""
    lines = []
    for i, (model, tenant, key, dep) in enumerate(combos):
        L = (
            f'api_key_id="{key}",model_alias="{model}",operation="chat.completions",'
            f'tenant_id="{tenant}",endpoint_id="1",deployment_id="{dep}",engine="vllm",upstream_type="local"'
        )
        n = base + i * 3 + 7
        lines.append(f'llmpod_proxy_requests_total{{{L},status="success"}} {n}')
        for kind in ("prompt", "completion", "total", "cached"):
            lines.append(f'llmpod_proxy_tokens_total{{{L},kind="{kind}"}} {n}')
        lines.append(f"llmpod_proxy_retries_total{{{L}}} {n}")
        lines.append(f"llmpod_proxy_stream_requests_total{{{L}}} {n}")
        for le in (*BUCKETS, "+Inf"):
            lines.append(f'llmpod_proxy_request_latency_ms_bucket{{{L},le="{le}"}} {n}')
        lines.append(f"llmpod_proxy_request_latency_ms_sum{{{L}}} {n * 400}")
        lines.append(f"llmpod_proxy_request_latency_ms_count{{{L}}} {n}")
        for le in (*BUCKETS, "+Inf"):
            lines.append(f'llmpod_proxy_ttft_ms_bucket{{{L},le="{le}"}} {n}')
        lines.append(f"llmpod_proxy_ttft_ms_sum{{{L}}} {n * 100}")
        lines.append(f"llmpod_proxy_ttft_ms_count{{{L}}} {n}")
    return "\n".join(lines)


def make_combos(models, tenants):
    return [(f"m{m}", f"t{t}", str(100 + t), f"dep-{m}") for m in range(models) for t in range(tenants)]


def cfg():
    return Settings(
        metrics_scrape_targets="http://t1/metrics",
        metrics_scrape_interval=5,
        metrics_scrape_interval_proxy=5,
        metrics_scrape_interval_engine_fast=5,
        metrics_scrape_interval_engine_slow=30,
        metrics_token="tok",
    )


def bench_ingest():
    print("== A. proxy 每轮 ingest 成本（parse+diff+攒桶+热层收口）==")
    for n_combos in (50, 200, 500):
        combos = make_combos(models=n_combos // 5, tenants=5)
        s = MetricsScraper(cfg())
        now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=TZ)
        # 首见建基线
        s._ingest("http://t1/metrics", build_text(combos, 0), now)
        s._close_round(now, ok=True)
        rounds = []
        for r in range(1, 8):
            text = build_text(combos, r * 1000)
            t0 = time.perf_counter()
            s._ingest("http://t1/metrics", text, now + timedelta(seconds=5 * r))
            s._close_round(now + timedelta(seconds=5 * r), ok=True)
            rounds.append((time.perf_counter() - t0) * 1000)
        rounds_sorted = sorted(rounds)
        median = rounds_sorted[len(rounds) // 2]
        text_kb = len(build_text(combos, 0)) / 1024
        # parse-only 对照
        text = build_text(combos, 9999)
        t0 = time.perf_counter()
        for _ in range(3):
            sum(1 for _ in text_string_to_metric_families(text))
        parse_ms = (time.perf_counter() - t0) / 3 * 1000
        samples = n_combos * 37
        print(
            f"  {n_combos:>4} 组合 ({samples:>5} 样本/轮, {text_kb:6.1f} KB): "
            f"整轮 {median:6.1f} ms | 仅解析 {parse_ms:5.1f} ms | "
            f"CPU 占比@5s {median / 5000 * 100:5.2f}%"
        )


def bench_memory():
    print("== B. 热层存储内存（125 key × 1440 点，上界形态）==")
    tm_start()
    rings = {}
    base = datetime(2026, 9, 14, 10, 0, 0, tzinfo=TZ)
    for k in range(125):
        d = deque(maxlen=1440)
        for i in range(1440):
            d.append(
                _HotPoint(
                    ts=base + timedelta(seconds=5 * i),
                    dur=5.0,
                    requests=i % 997,
                    errors=i % 53,
                    rejected=i % 29,
                    prompt_tokens=i * 131,
                    completion_tokens=i * 37,
                    total_tokens=i * 168,
                    cached_tokens=i * 61,
                    latency_sum_ms=i * 977,
                    latency_count=i % 89,
                    ttft_sum_ms=i * 311,
                    ttft_count=i % 47,
                    in_flight=i % 31,
                )
            )
        rings[k] = d
    current, _peak = get_traced_memory()
    tm_stop()
    total_mb = current / 1024 / 1024
    per_point = current / (125 * 1440)
    print(f"  合计 {total_mb:6.1f} MB | 每点 {per_point:6.0f} B | 点数 {125 * 1440:,}")
    # 现实形态参考：~56 key
    realistic = per_point * 56 * 1440 / 1024 / 1024
    print(f"  现实参考（~56 key：10 模型+5 部署+20 租户+20 Key+__all__）：{realistic:5.1f} MB")


def _point(ts, dur=5.0, **kw):
    # 直接构造真实 _HotPoint：替身字段集会随实体漂移，漂移后量出的聚合
    # 成本就不再代表线上路径
    base = dict(
        requests=3,
        errors=0,
        rejected=0,
        prompt_tokens=450,
        completion_tokens=150,
        total_tokens=600,
        cached_tokens=200,
        latency_sum_ms=2100,
        latency_count=3,
        ttft_sum_ms=900,
        ttft_count=1,
        in_flight=7,
    )
    base.update(kw)
    return _HotPoint(ts=ts, dur=dur, **base)


def bench_realtime_read():
    print("== C. realtime 读路径（热层→降采样→构成折叠，单次请求的聚合部分）==")
    now = datetime(2026, 9, 14, 14, 0, 0, tzinfo=TZ)
    span = 2 * 3600
    all_points = [_point(now - timedelta(seconds=span - 5 * i)) for i in range(span // 5)]
    key_points = {f"m{i}": list(all_points) for i in range(25)}
    from_ = now - timedelta(hours=1)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        cur = [p for p in all_points if from_ <= p.ts < now]
        total_slots = slot_hot_points(cur, from_, now, 30.0)
        key_slots = {k: slot_hot_points(pts, from_, now, 30.0) for k, pts in key_points.items()}
        series = key_slot_tpm(key_slots, total_slots)
        req_series = key_slot_requests(key_slots, total_slots)
        trend, tpm_series = trend_payload(total_slots)
        folded = fold_composition(key_slots, series, req_series, total_slots, tpm_series)
        assert folded["items"]
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    print(f"  25 key × 7200 点（2h 环形全扫）: 中位 {times[2]:6.1f} ms/请求")


def bench_engine_parse():
    print("== D. 引擎 /metrics 大文本（~500 族非白名单 + 少量白名单）==")
    lines = []
    for f in range(500):
        name = f"vllm:some_metric_{f}"
        lines.append(f"# TYPE {name} gauge")
        for j in range(3):
            lines.append(f'{name}{{shard="{j}"}} {f}.{j}')
    # 白名单指标（真正会被采集的）
    for name in ("vllm:kv_cache_usage_perc", "vllm:num_requests_running", "vllm:num_requests_waiting"):
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} 0.42")
    text = "\n".join(lines)
    s = MetricsScraper(cfg())
    from llmpod.apps.webserver.metrics_scraper import _EngineTarget

    t = _EngineTarget(url="http://eng:8000/metrics", instance_id="i-1", deployment_id="d1", engine="vllm")
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=TZ)
    s._ingest_engine_text(t, text, now)  # 首见
    times = []
    for r in range(5):
        t0 = time.perf_counter()
        s._ingest_engine_text(t, text.replace("0.42", f"0.4{r}"), now)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    t0 = time.perf_counter()
    for _ in range(3):
        sum(1 for _ in text_string_to_metric_families(text))
    parse_ms = (time.perf_counter() - t0) / 3 * 1000
    print(
        f"  文本 {len(text) / 1024:.0f} KB / ~1500 样本: 整轮 {times[2]:6.1f} ms | 仅解析 {parse_ms:5.1f} ms"
        f" | 快档@5s 占比 {times[2] / 5000 * 100:.2f}% / 慢档@30s {times[2] / 30000 * 100:.2f}%（×单实例）"
    )


bench_ingest()
bench_memory()
bench_realtime_read()
bench_engine_parse()
