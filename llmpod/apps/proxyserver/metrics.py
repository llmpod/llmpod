"""API 网关管道指标（prometheus_client 模块级单例）。

埋点原则：一次请求只打一个点——收尾出口（成功/失败/中断）或拒绝出口二选一；
业务管道只调本模块的打点函数，不感知差分/物化/存储的存在。标签值统一 str
（数值 ID 转字符串，None→空串），指标名带 ``llmpod_proxy_`` 前缀，由
``/metrics`` 端点渲染为 Prometheus 文本、中央 scraper 抓取。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from llmpod.apps.proxyserver.usage_cache import extract_cached_tokens, usage_totals

# 基础维度标签：拒绝发生在选路之前，选路后维度（endpoint/deployment/engine/
# upstream_type）为空串——聚合层按有值标签 group，空标签单独成组（「未选路」桶）。
_BASE_LABELS = (
    "tenant_id",
    "api_key_id",
    "model_alias",
    "endpoint_id",
    "deployment_id",
    "engine",
    "operation",
    "upstream_type",
)

# 推理延迟直方图桶（毫秒，对数-ish 覆盖 10ms~60s；TTFT 同桶）
_LATENCY_BUCKETS = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)

requests_total = Counter(
    "llmpod_proxy_requests_total",
    "管道出口请求数（含拒绝）",
    (*_BASE_LABELS, "status"),
)
rejects_total = Counter(
    "llmpod_proxy_rejects_total",
    "准入拒绝数（按原因分类）",
    (*_BASE_LABELS, "reject_reason"),
)
redis_degraded_total = Counter(
    "llmpod_proxy_redis_degraded_total",
    "Redis 熔断降级放行的请求数（限流/配额未执行）",
)
retries_total = Counter(
    "llmpod_proxy_retries_total",
    "failover 重试次数（首试成功=0）",
    _BASE_LABELS,
)
tokens_total = Counter(
    "llmpod_proxy_tokens_total",
    "token 计数（kind=prompt/completion/total/cached）",
    (*_BASE_LABELS, "kind"),
)
request_latency_ms = Histogram(
    "llmpod_proxy_request_latency_ms",
    "端到端延迟（毫秒）",
    _BASE_LABELS,
    buckets=_LATENCY_BUCKETS,
)
ttft_ms = Histogram(
    "llmpod_proxy_ttft_ms",
    "流式首帧延迟（毫秒；非流式无样本）",
    _BASE_LABELS,
    buckets=_LATENCY_BUCKETS,
)
in_flight_requests = Gauge(
    "llmpod_proxy_in_flight_requests",
    "当前在途请求数（管道入口+1、所有出口-1）",
)
stream_requests_total = Counter(
    "llmpod_proxy_stream_requests_total",
    "流式请求数（含中断）",
    _BASE_LABELS,
)

# 打点函数收口的标签顺序（与 _BASE_LABELS 一致），调用方传 dict 免位置错位
_LABEL_ORDER = _BASE_LABELS


def _norm_labels(labels: dict) -> dict:
    """标签值规范化：None/数值 → str；model_alias 截 255（与 request_log 同限）。"""
    out = {}
    for key in _LABEL_ORDER:
        val = labels.get(key)
        if val is None:
            out[key] = ""
        elif key == "model_alias":
            out[key] = str(val)[:255]
        else:
            out[key] = str(val)
    return out


def enter_request() -> None:
    """管道入口：在途 gauge +1。"""
    in_flight_requests.inc()


def exit_request() -> None:
    """管道出口（所有出口，含拒绝/异常）：在途 gauge -1。"""
    in_flight_requests.dec()


def record_reject(reason: str, labels: dict) -> None:
    """拒绝打点：requests_total(status=rejected) + rejects_total(reason)。"""
    norm = _norm_labels(labels)
    requests_total.labels(**norm, status="rejected").inc()
    rejects_total.labels(**norm, reject_reason=reason).inc()


def record_outcome(
    labels: dict, *, status: str, usage: dict | None, latency_ms: int | None, retry_count: int = 0, stream: bool = False
) -> None:
    """收尾出口打点（成功/失败/中断共用；labels 含选路后的完整维度）。

    latency_ms 为空（异常路径未计时）时跳过直方图样本；usage 为空跳过 token 计数。
    """
    norm = _norm_labels(labels)
    requests_total.labels(**norm, status=status).inc()
    if retry_count > 0:
        retries_total.labels(**norm).inc(retry_count)
    if stream:
        stream_requests_total.labels(**norm).inc()
    if usage:
        prompt, completion, total = usage_totals(usage)
        # 缓存命中 token：各引擎嵌套形状不一（OpenAI prompt_tokens_details.cached_tokens /
        # Anthropic cache_read_input_tokens / 顶层 cached_tokens），统一走既有抽取器
        cached = extract_cached_tokens(usage) or 0
        if prompt:
            tokens_total.labels(**norm, kind="prompt").inc(prompt)
        if completion:
            tokens_total.labels(**norm, kind="completion").inc(completion)
        if total:
            tokens_total.labels(**norm, kind="total").inc(total)
        if cached:
            tokens_total.labels(**norm, kind="cached").inc(cached)
    if latency_ms is not None:
        request_latency_ms.labels(**norm).observe(latency_ms)


def record_ttft(labels: dict, ttft_ms_value: int) -> None:
    """TTFT 打点（流式首个事件时调用一次）。"""
    ttft_ms.labels(**_norm_labels(labels)).observe(ttft_ms_value)


def outcome_labels(ctx, entry, target, operation: str) -> dict:
    """从管道上下文组装完整维度标签（收尾出口用；与 build_record 同源字段）。"""
    return {
        "tenant_id": ctx.tenant_id if ctx is not None else None,
        "api_key_id": ctx.key_id if ctx is not None else None,
        "model_alias": entry.name if entry is not None else None,
        "endpoint_id": target.endpoint_id if target is not None else None,
        "deployment_id": target.deployment_id if target is not None else None,
        "engine": target.engine_type if target is not None else None,
        "operation": operation,
        "upstream_type": target.upstream_type if target is not None else None,
    }
