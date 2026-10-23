"""中央指标 Scraper：周期抓取各 /metrics 端点 → counter 差分 → 5min 桶 → PG。

Pull 模式的中央聚合器（webserver 进程内常驻后台任务）：

- 目标来源两路：
  1. ``LLMPOD_METRICS_SCRAPE_TARGETS``（CSV）：proxyserver 管道指标（多
     proxyserver 实例=多 URL，各自差分各自行，聚合交给查询层）；
  2. DB 发现：每轮查运行中的 Instance（READY/RUNNING，spec.port 非空）join
     Node.ip 生成 ``http://{node_ip}:{port}/metrics``——引擎（vLLM/TGI/SGLang）
     的 /metrics 与 OpenAI API 同端口，管理端能转发推理就能抓指标，无需 node
     代采（node 只抓 127.0.0.1 的方案已废弃）；
- counter 差分：每 (target, sample) 记上次值，增量 = 本次 − 上次；回绕/重启
  （本次 < 上次）→ 增量 = 本次（当作从零开始）；
- 5min 桶（本地时区网格）：增量累加进当前桶，桶边界翻转时批量 INSERT
  metric_rollup；引擎 gauge 按 avg/max/samples 累计进独立的
  metric_engine_rollup（gauge 看常态与峰值、抢占 counter 看桶内增量）；
- 引擎指标在 ingest 层做「原始名 → 归一名」白名单映射（vLLM 指标名随版本改名，
  新旧名都接受），并保留每实例 last-value 快照供实时面板直读；
- 抓取频率双计时器：API 网关管道指标固定档（持久数据源采样密度需稳定）；引擎
  指标按需双档——实时面板被消费（路由 touch_demand 触碰）时快档，无人消费
  降慢档心跳（跨节点抓取 × 引擎侧全量 collect 的成本只在有人看时花）；
- 热层（进程内存）：每轮 proxy 差分按维度（全部/模型/部署/租户/API Key）聚合
  成点，环形缓冲保留窗口见 _RING_RETENTION_SECONDS（__all__/直方图 2h 支撑
  近 1h 窗口 + 同长度上一窗口环比；构成维度只有当前窗口消费，环深取半以省
  一半内存）；全局直方图逐轮增量单独留痕，供滑动窗分位数插值；
- 保留期清理：同循环内低频顺带删超期桶。

抓取/落库任何失败只 log 不抛（监控链路绝不反向影响管理端）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from prometheus_client.parser import text_string_to_metric_families

from llmpod.config.settings import Settings
from llmpod.core.taskwatch import BackgroundTaskManager
from llmpod.models import MetricRollup
from llmpod.utils.time import now_local

log = logging.getLogger("llmpod.apps.webserver.metrics_scraper")

_BUCKET_SECONDS = 300
_RETENTION_SWEEP_INTERVAL = 3600
# 引擎 target 发现的 DB 轮询间隔（秒）；实例列表变化频率低，不必每轮都查
_TARGET_DISCOVERY_INTERVAL = 60.0

# 引擎快档的需求信号窗口（秒）：消费端点被触碰后窗口内保持快档。必须大于前端
# 轮询间隔（5s），容忍丢一两次请求，防「关页面→降档→再打开→升档」抖动
_DEMAND_WINDOW = 12.0

# 热层环形缓冲目标保留时长（秒）：__all__ 与全局直方图按 2h 保留（近 1h
# 窗口 + 同长度上一窗口环比）；构成维度的环只有当前窗口消费（环比只读
# __all__），按 1h + 余量取深度，省掉永远读不到的另一半
_RING_RETENTION_SECONDS = 2 * 3600
_DIM_RING_RETENTION_SECONDS = _RING_RETENTION_SECONDS // 2
# 单维度热层 key 上界：超出后新 key 并入 "__other__"（长跑进程内存有界）
_HOT_MAX_KEYS = 24
# 长尾折叠桶名——查询层 fold_composition 的 _OTHER_KEY 必须与之一致（有单测
# 对账）；两侧字面量改成不一样的值会让长尾被当成普通序列参与 top-5 排名
_HOT_OTHER_KEY = "__other__"
# 热层 key 闲置修剪阈值（秒）。必须不小于实时端点的最大窗口（1h）——被修剪的
# key 其全部点都已滑出当前窗口，否则间歇流量的 key 会在窗口内静默消失
_HOT_IDLE_SECONDS = 3600

# 差分基线闲置修剪阈值（秒）。存活性判据是「距上次出现的时长」而非「本轮是否
# 出现」：proxy 轮与引擎轮节拍不同（proxy 档调大后两个 proxy 轮之间夹着引擎
# 节拍）、单 target 漏抓一轮，都会让键本轮缺席——按轮缺席清基线会把漏抓期间
# 的累积当首见丢弃，且基线已没了、无法自愈
_DIFF_BASELINE_IDLE_SECONDS = 3600.0

# 席位满员时新 key 顶替静默在座 key 的阈值（秒）。既要比正常漏抓的空隙宽（偶
# 发丢一轮不换座），也要比闲置修剪（1h）窄——等不到顶替就被修剪了
_HOT_EVICT_IDLE = 600.0

# proxy 指标族前缀
_PROXY_PREFIX = "llmpod_proxy_"

# 热层维度名 → proxy 标签名（构成面板/趋势分组用；值为空的样本只进 __all__）
_HOT_DIM_LABELS = (
    ("model", "model_alias"),
    ("deployment", "deployment_id"),
    ("tenant", "tenant_id"),
    ("api_key", "api_key_id"),
)

# 引擎原始指标名 → 归一名（白名单映射；未列出的引擎指标不采集）。
# vLLM ≥0.9.2 用 kv_cache_usage_perc，旧版叫 gpu_cache_usage_perc，两者都接受；
# SGLang 指标名来自 sglang 0.5.x 实测。TGI 暂无公认 /metrics 口径，留空。
_ENGINE_METRIC_MAP: dict[str, tuple[tuple[str, str], ...]] = {
    "vllm": (
        ("vllm:kv_cache_usage_perc", "kv_cache_util"),
        ("vllm:gpu_cache_usage_perc", "kv_cache_util"),
        ("vllm:num_requests_running", "running_requests"),
        ("vllm:num_requests_waiting", "waiting_requests"),
        ("vllm:prefix_cache_hit_rate", "prefix_cache_hit_rate"),
        ("vllm:num_preemptions", "num_preemptions"),
    ),
    "sglang": (
        ("sglang:token_usage", "kv_cache_util"),
        ("sglang:num_running_reqs", "running_requests"),
        ("sglang:num_queue_reqs", "waiting_requests"),
        ("sglang:cache_hit_rate", "prefix_cache_hit_rate"),
    ),
}

# 引擎 gauge 归一名 → metric_engine_rollup 列名（avg/max 成对）
_ENGINE_GAUGE_COLUMNS = (
    "kv_cache_util",
    "running_requests",
    "waiting_requests",
    "prefix_cache_hit_rate",
)

# 映射前剥离的 counter/histogram 后缀（基名匹配，如 vllm:num_preemptions_total）
_METRIC_NAME_SUFFIXES = ("_total", "_sum", "_count", "_bucket")


def _map_engine_metric(engine: str, name: str) -> str | None:
    """引擎原始指标名（family 名或样本名）→ 归一名；白名单外返回 None 不采集。

    指标名可能带 counter/histogram 后缀，映射按剥离后缀后的基名匹配。
    """
    for suffix in _METRIC_NAME_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    for src, dst in _ENGINE_METRIC_MAP.get(engine, ()):
        if src == name:
            return dst
    return None


def _bucket_floor(ts: datetime) -> datetime:
    """系统本地时区对齐 5min 网格的桶起点（部署期 TZ=Asia/Shanghai）。"""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % _BUCKET_SECONDS, tz=ts.tzinfo or ts.astimezone().tzinfo)


def _to_int(val: str) -> int | None:
    """标签值 → int（空串/None → None；非法值 None，不抛）。"""
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def _finite_or_none(v: float | None) -> float | None:
    """非有限值（NaN/±Inf）按缺失处理。

    prometheus 文本格式允许 NaN/Inf 样本，混进快照会让 JSON 序列化直接 500
    （allow_nan=False）、引擎桶 avg/max 落库写出 NaN——在采集入口统一拦掉。
    """
    if v is None or v != v or v == float("inf") or v == float("-inf"):
        return None
    return float(v)


@dataclass
class _EngineTarget:
    """DB 发现的引擎抓取目标（一个 READY/RUNNING 实例 = 一个 target）。"""

    url: str
    instance_id: str
    deployment_id: str
    engine: str


@dataclass
class EngineSnapshot:
    """单实例引擎实时指标快照（scraper last-value 缓存，实时面板直读）。

    ``ok=False`` 表示最近一轮抓取失败（引擎重启中/端口未就绪），其余字段无效；
    从未抓到过（新实例/已下线）不出现在缓存里，由路由侧兜底 ok=False。
    ``fetched_at`` 是墙上时钟（响应侧算 age_ms 标注数据新鲜度用）。引擎名不
    入快照——响应侧从 ModelDeployment.engine（权威来源）取。
    """

    instance_id: str
    deployment_id: str
    ok: bool = True
    kv_cache_util: float | None = None
    running_requests: float | None = None
    waiting_requests: float | None = None
    prefix_cache_hit_rate: float | None = None
    num_preemptions: float | None = None
    fetched_at: datetime | None = None


@dataclass
class _Accum:
    """一个 标签组合 在当前桶内的累计值（INSERT 前的内存形态）。"""

    labels: dict
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    retry_count: int = 0
    stream_requests: int = 0
    latency_sum_ms: int = 0
    latency_count: int = 0
    latency_buckets: list[int] = field(default_factory=lambda: [0] * 13)  # 12 边界+inf
    ttft_sum_ms: int = 0
    ttft_count: int = 0
    ttft_buckets: list[int] = field(default_factory=lambda: [0] * 13)
    in_flight_peak: int = 0


@dataclass(slots=True)
class _RoundAccum:
    """一个 (维度, key) 在当前 proxy 抓取轮内的增量（热层点的累加形态）。

    只存原始增量，速率/比率类派生量（qpm、命中率、错误率…）由查询层按点的
    dur 换算——间隔变化（档位、漏抓）只影响分母、口径天然正确。

    计数字段集与 _HotPoint 一致（_close_round 冻结时逐字段搬移）。
    """

    requests: int = 0
    errors: int = 0
    rejected: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    latency_sum_ms: int = 0
    latency_count: int = 0
    ttft_sum_ms: int = 0
    ttft_count: int = 0

    def add(
        self,
        *,
        requests: int = 0,
        errors: int = 0,
        rejected: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cached_tokens: int = 0,
        latency_sum_ms: int = 0,
        latency_count: int = 0,
        ttft_sum_ms: int = 0,
        ttft_count: int = 0,
    ) -> None:
        """把若干增量字段累进（0 值跳过——_ingest_proxy 按事件类型只带增量字段）。"""
        if requests:
            self.requests += requests
        if errors:
            self.errors += errors
        if rejected:
            self.rejected += rejected
        if prompt_tokens:
            self.prompt_tokens += prompt_tokens
        if completion_tokens:
            self.completion_tokens += completion_tokens
        if total_tokens:
            self.total_tokens += total_tokens
        if cached_tokens:
            self.cached_tokens += cached_tokens
        if latency_sum_ms:
            self.latency_sum_ms += latency_sum_ms
        if latency_count:
            self.latency_count += latency_count
        if ttft_sum_ms:
            self.ttft_sum_ms += ttft_sum_ms
        if ttft_count:
            self.ttft_count += ttft_count


@dataclass(slots=True)
class _HotPoint:
    """热层单点：一轮 proxy 抓取的聚合增量。

    ``dur`` = 距该维度上一个点的秒数（含漏抓跨度，速率按它摊）；``in_flight``
    只在 __all__ 维度上有意义（网关在途是全局 gauge，无标签）。
    """

    ts: datetime
    dur: float
    requests: int = 0
    errors: int = 0
    rejected: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    latency_sum_ms: int = 0
    latency_count: int = 0
    ttft_sum_ms: int = 0
    ttft_count: int = 0
    in_flight: int = 0


@dataclass
class _EngineAccum:
    """一个引擎实例在当前桶内的 gauge 统计（metric_engine_rollup 的内存形态）。

    gauge 记桶内 avg（sum/count）与 max；``samples`` 是桶内成功抓取次数，用于
    区分「真的没变」与「没抓到」；``preemptions`` 是 counter 的桶内增量。
    """

    instance_id: str
    deployment_id: str
    engine: str
    samples: int = 0
    preemptions: int = 0
    sums: dict[str, float] = field(default_factory=dict)
    maxes: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def add_gauge(self, name: str, value: float) -> None:
        self.sums[name] = self.sums.get(name, 0.0) + value
        self.maxes[name] = max(self.maxes.get(name, value), value)
        self.counts[name] = self.counts.get(name, 0) + 1

    def avg(self, name: str) -> float | None:
        count = self.counts.get(name, 0)
        return round(self.sums[name] / count, 4) if count else None

    def max_of(self, name: str) -> float | None:
        return round(self.maxes[name], 4) if name in self.maxes else None


@dataclass
class _TargetHealth:
    """单抓取目标的健康记账（scrape-health 端点数据源）。"""

    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_duration_ms: float | None = None


class MetricsScraper:
    """抓取 + 差分 + 攒桶 + 热层 + 落库的状态机（每实例一个后台循环）。"""

    def __init__(self, cfg: Settings, *, client: httpx.AsyncClient | None = None):
        self._cfg = cfg
        self._targets = [t.strip() for t in cfg.metrics_scrape_targets.split(",") if t.strip()]
        self._client = client  # 测试注入；生产循环内自建
        # 抓取档位（≥1 收敛与慢档≥快档由 Settings 校验器单处保证，这里直读）
        self._proxy_interval = cfg.metrics_scrape_interval_proxy
        self._engine_fast = cfg.metrics_scrape_interval_engine_fast
        self._engine_slow = cfg.metrics_scrape_interval_engine_slow
        # (target, sample_name, labelkey) -> (上次值, 上次见到的 monotonic 时间)。
        # counter 差分基线；修剪按闲置时长判（见 _prune_diff_baselines）
        self._last: dict[tuple, tuple[float, float]] = {}
        # (target, family, labelkey) -> _Accum
        self._buckets: dict[tuple, _Accum] = {}
        self._bucket_start: datetime | None = None
        self._last_sweep = 0.0
        # 桶翻转时由 _ensure_bucket 入队、_maybe_flush 成功落库后出队；
        # 落库失败保留队头下轮重试（INSERT 幂等重放，跨多桶故障不丢桶）
        self._flush_queue: deque[tuple[datetime, dict]] = deque()
        self._last_discovery = 0.0
        # DB 发现的引擎 targets：instance_id -> _EngineTarget
        self._engine_targets: dict[str, _EngineTarget] = {}
        # 引擎实时快照：instance_id -> EngineSnapshot（实时面板直读；未抓到无条目）
        self._engine_snapshots: dict[str, EngineSnapshot] = {}
        # 引擎抢占 counter 差分基线：instance_id -> 上次值。独立于 _last（不参与
        # 每桶修剪——引擎按慢档抓时键会隔桶才出现，进了修剪链路会永远首见归零）
        self._engine_preempt_last: dict[str, float] = {}
        # 引擎桶累计：instance_id -> _EngineAccum（当前桶）；翻转入独立落库队列
        self._engine_buckets: dict[str, _EngineAccum] = {}
        self._engine_flush_queue: deque[tuple[datetime, dict[str, _EngineAccum]]] = deque()
        # ---- 热层（实时端点数据源，全部进程内存）----
        # dim -> key -> deque[_HotPoint]；__all__ 维度单 key ""，承载全网关总量
        self._hot: dict[str, dict[str, deque[_HotPoint]]] = {}
        # 当前轮增量：dim -> key -> _RoundAccum（每轮重建）
        self._round: dict[str, dict[str, _RoundAccum]] = {}
        # 全局直方图逐轮增量（分位数滑动窗插值用）：轮内累加、轮末冻结进环形
        from llmpod.apps.proxyserver.metrics import _LATENCY_BUCKETS

        self._hist_len = len(_LATENCY_BUCKETS) + 1
        self._hist_round: tuple[list[int], list[int]] = ([0] * self._hist_len, [0] * self._hist_len)
        # 环深下限 = 保留时长满覆盖所需点数：窄间隔（如 1s）时 2h 需 7200 点，
        # 上限 4000 会截成 66min——环比窗就残缺了；下限 600 只防超宽间隔白占
        # 内存。构成维度环深按 1h 消费窗（见 _DIM_RING_RETENTION_SECONDS）
        self._ring_maxlen = max(600, int(_RING_RETENTION_SECONDS / self._proxy_interval) + 1)
        self._dim_ring_maxlen = max(600, int(_DIM_RING_RETENTION_SECONDS / self._proxy_interval) + 2)
        self._hist_ring: deque[tuple[datetime, list[int], list[int]]] = deque(maxlen=self._ring_maxlen)
        self._last_round_at: datetime | None = None
        # 在途 gauge 按 target 记最新值（多 proxyserver 实例求和为全网关在途）；
        # 抓取失败的轮次清掉该 target 项——下线实例的残值不能永久计入总量
        self._in_flight_by_target: dict[str, int] = {}
        # ---- 按需升降档 ----
        self._last_demand: float | None = None  # monotonic；None = 从未被消费
        self._engine_tier: int | None = None
        # ---- 抓取链路自监控（scrape-health 数据源）----
        self._target_health: dict[str, _TargetHealth] = {}

    # ---- 主循环（双计时器：proxy 固定档 / 引擎按需双档） ----

    async def run(self) -> None:
        """后台循环：各自到点才抓 → 差分 → 攒桶/热层 → 翻转时落库。

        睡眠步长取当前两档的最小值——proxy 快档时它就是节拍器；引擎升档后
        大概率已超期，下一拍立刻补抓（打开大屏后约一个快档周期进入实时跟随）。
        """
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(2.0))
        last_proxy = last_engine = float("-inf")
        try:
            while True:
                proxy_iv = self._proxy_interval
                engine_iv = self._engine_interval()
                await asyncio.sleep(min(proxy_iv, engine_iv))
                now_mono = time.monotonic()
                now = now_local()
                try:
                    if now_mono - last_proxy >= proxy_iv:
                        await self._scrape_proxy_round(client, now)
                        last_proxy = now_mono
                    if now_mono - last_engine >= engine_iv:
                        await self._scrape_engines(client, now)
                        last_engine = now_mono
                    await self._maybe_flush(now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("metrics scrape round failed")
        finally:
            if own_client:
                await client.aclose()

    async def _scrape_proxy_round(self, client: httpx.AsyncClient, now: datetime) -> None:
        """一轮 proxy 抓取：全部静态 target 并发抓 → 差分入桶 → 收口热层点。

        并发与引擎轮同因：单 target 2s 超时，串行会在 target 数 ≳3（多副本
        网关）时吃满抓取间隔，热层点距与 age_ms 随最慢 target 漂移。
        """
        # return_exceptions 兜意外路径（_fetch 已自吞常见错误）；入桶仍按
        # target 顺序补齐，差分结果与串行完全一致
        texts = await asyncio.gather(
            *(self._fetch(client, target) for target in self._targets),
            return_exceptions=True,
        )
        targets_ok = 0
        for target, text in zip(self._targets, texts):
            if isinstance(text, BaseException):
                log.warning("proxy scrape task failed unexpectedly: %r", text)
                text = None
            if text is None:
                # 抓不到就不知道该 target 的在途值——清掉而非沿用残值
                # （下线/重启的 proxyserver 实例会把最后读数永久计入全网关在途）
                self._in_flight_by_target.pop(target, None)
                continue
            targets_ok += 1
            self._ingest(target, text, now)
        self._close_round(now, ok=targets_ok > 0)

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        target: str,
        *,
        with_token: bool = True,
        health_key: str | None = None,
    ) -> str | None:
        """抓单 target；``health_key`` 是健康记账的键（默认 target URL）。

        引擎 target 传 instance_id——引擎 URL 由 ip:port 拼出、端口分配总给
        最小空闲端口，实例重启常拿回同一 URL，按 URL 记会让新实例继承死掉
        实例的失败计数（scrape-health 误报「一直在失败」）。
        """
        headers = None
        if with_token:
            # 凭据最小暴露：token 只发给声明需要鉴权的目标（proxyserver /metrics），
            # 不随请求发往任意节点侧引擎端点（多租户拓扑下 node 侧不可全信）
            headers = {"Authorization": f"Bearer {self._cfg.metrics_token}"}
        started = time.monotonic()
        try:
            resp = await client.get(target, headers=headers)
            resp.raise_for_status()
            self._record_health(health_key or target, ok=True, elapsed_ms=(time.monotonic() - started) * 1000)
            return resp.text
        except Exception as e:
            self._record_health(health_key or target, ok=False, elapsed_ms=(time.monotonic() - started) * 1000)
            log.warning("metrics scrape failed (target=%s): %r", target, e)
            return None

    # ---- 按需升降档（需求信号 = 路由侧消费即触碰） ----

    def touch_demand(self) -> None:
        """实时面板被消费：触碰需求信号，引擎抓取在窗口期内保持快档。"""
        self._last_demand = time.monotonic()

    def _engine_interval(self) -> int:
        """当前引擎抓取间隔：需求窗口内快档，否则慢档心跳。切换打一条 log。"""
        tier = self._engine_fast
        if self._last_demand is None or time.monotonic() - self._last_demand >= _DEMAND_WINDOW:
            tier = self._engine_slow
        if tier != self._engine_tier:
            self._engine_tier = tier
            log.info(
                "engine scrape interval -> %ss (%s)",
                tier,
                "fast: realtime panel in use" if tier == self._engine_fast else "slow: no consumer",
            )
        return tier

    async def _scrape_engines(self, client: httpx.AsyncClient, now: datetime) -> None:
        """发现（DB 轮询）+ 抓取全部运行中引擎实例的 /metrics。

        各 target 并发抓（gather）：单台 2s 超时下，串行会在实例数 ≳8 时
        吃满抓取间隔、拉长差分基线窗口，桶速率随之失真。
        """
        if time.monotonic() - self._last_discovery >= _TARGET_DISCOVERY_INTERVAL:
            self._last_discovery = time.monotonic()
            discovered = await self._discover_engine_targets()
            # 空 dict 有两义，用哨兵区分：查询本身失败返回 None（DB 抖动，保留
            # 旧 target 集——一次抖动不该清空抓取目标，快照 ok=True 的旧值比
            # 「全停 60s」更接近真值）；查询成功但 0 实例返回 {}（部署全停，
            # 目标集与快照一并清——继续抓已删实例只会产出幻影失败与健康噪声）
            if discovered is not None:
                self._engine_targets = discovered
        if not self._engine_targets:
            return
        # return_exceptions：单 target 的意外异常不炸掉其余引擎抓取与本轮落库
        # （_fetch/_ingest_engine_text 自吞常见错误，这里兜底层外的意外路径）
        results = await asyncio.gather(
            *(self._scrape_engine_one(client, t, now) for t in self._engine_targets.values()),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                log.warning("engine scrape task failed unexpectedly: %r", r)

    async def _scrape_engine_one(self, client: httpx.AsyncClient, t: _EngineTarget, now: datetime) -> None:
        # 引擎 /metrics 无鉴权（vLLM/TGI 默认），with_token=False：凭据不出管理端；
        # 健康按实例记（URL 会因端口复用漂移到别的新实例）
        text = await self._fetch(client, t.url, with_token=False, health_key=t.instance_id)
        if text is None:
            # 抓取失败 → 快照标 ok=False（保留上次值，面板能区分「旧值」与「从未有」）
            snap = self._engine_snapshots.get(t.instance_id)
            if snap is not None:
                snap.ok = False
            return
        self._ingest_engine_text(t, text, now)

    async def _discover_engine_targets(self) -> dict[str, _EngineTarget] | None:
        """从 DB 查运行中实例 → 引擎 /metrics target（spec.port + node.ip）。

        引擎 /metrics 与 OpenAI API 同端口（host 网络），能被 proxy 转发就能被
        抓取；DB 里 port 在 Instance.spec JSONB 内（InstanceSpec.port）。

        查询失败返回 None（调用方据此保留旧 target 集），成功返回 dict（可为
        空 = 部署全停，此时一并清快照——面板应显示缺失而非旧值）。
        """
        from sqlalchemy import select

        from llmpod.models import Instance, ModelDeployment, Node

        out: dict[str, _EngineTarget] = {}
        try:
            from llmpod.db import get_db_context

            async with get_db_context() as db:
                rows = list(
                    (
                        await db.execute(
                            select(Instance, Node.ip, ModelDeployment.engine)
                            .join(Node, Node.id == Instance.assigned_node)
                            .join(ModelDeployment, ModelDeployment.deployment_id == Instance.deployment_id)
                            .where(Instance.status.in_(("RUNNING", "READY")))
                        )
                    ).all()
                )
        except Exception:
            log.exception("engine targets discovery failed")
            # 异常路径返回 None：调用方保留旧 target 集，快照不动——一次 DB
            # 抖动不应清空面板的 last-known-good
            return None
        for inst, node_ip, engine in rows:
            # endpoint 为空 = PP 多机 headless worker（无 API server，/metrics 也不存在）
            # 或就绪信息未齐的实例：抓了必然失败，还会让 scrape-health 永远挂一条红记录。
            # spec.port 不能当判据——worker 的 spec 同样带端口（vllm serve 参数里就有）。
            if not inst.endpoint:
                continue
            spec = inst.spec or {}
            port = spec.get("port")
            if not port or not node_ip:
                continue  # 未分配节点 / spec 缺端口
            out[inst.instance_id] = _EngineTarget(
                url=f"http://{node_ip}:{port}/metrics",
                instance_id=inst.instance_id,
                deployment_id=inst.deployment_id,
                engine=engine or "",
            )
        # 离线实例：快照与差分基线一并清（部署停了面板应显示缺失而非旧值）
        stale = set(self._engine_snapshots) - set(out)
        for sid in stale:
            self._engine_snapshots.pop(sid, None)
            self._engine_preempt_last.pop(sid, None)
        # 健康记账只留「现存 proxy URL ∪ 现存引擎实例」——下线实例的失败
        # 计数不清会让历史故障永远挂在 scrape-health 上
        keep_health = set(self._targets) | set(out)
        for key in [k for k in self._target_health if k not in keep_health]:
            del self._target_health[key]
        return out

    # ---- 解析与差分 ----

    def _ingest(self, target: str, text: str, now: datetime) -> None:
        """解析文本 → counter 差分 / gauge 快照 → 攒进当前桶。

        解析逐 family 隔离：单段畸形样本只丢该段（log 后继续），不让一个
        坏响应炸掉整轮抓取（引擎 target 还排在后面等着一轮的剩余时间）。
        """
        self._ensure_bucket(now)
        try:
            families = list(text_string_to_metric_families(text))
        except Exception as e:
            log.warning("metrics parse failed (target=%s): %r", target, e)
            return
        for family in families:
            try:
                for sample in family.samples:
                    self._ingest_sample(target, family.name, sample, now)
            except Exception as e:
                log.warning("metrics sample ingest failed (target=%s family=%s): %r", target, family.name, e)

    def _ingest_sample(self, target: str, family: str, sample, now: datetime) -> None:
        sname, labels, value = sample.name, dict(sample.labels), sample.value
        if family.startswith(_PROXY_PREFIX) or sname.startswith(_PROXY_PREFIX):
            self._ingest_proxy(target, family, sname, labels, value, now)

    def _ingest_proxy(self, target: str, family: str, sname: str, labels: dict, value: float, now: datetime) -> None:
        """proxyserver 管道指标族：counter 差分 + histogram 子指标。

        每个增量同时进两处：5min 桶累计器（落库）与当前轮聚合（热层）——
        两者共用一次差分结果，速率与总量口径天然一致。
        """
        # in_flight 是无标签 gauge（非累计），必须先于 counter 差分口处理：
        # 差分口对首见样本返回 0 且丢弃下降值，会把 gauge 削成「只增序列」。
        # 单值记到该 target 全部累计器的 in_flight_peak（桶内峰值）；NaN/Inf
        # 解析器可能产出，int() 会抛 → 先行过滤。本轮尚无桶时暂丢（下轮起必有）。
        if sname == "llmpod_proxy_in_flight_requests":
            if value != value or value in (float("inf"), float("-inf")):
                return
            self._in_flight_by_target[target] = int(value)
            for acc in self._buckets_for_target(target):
                acc.in_flight_peak = max(acc.in_flight_peak, int(value))
            return

        # 直方图子指标：<name>_bucket（带 le 标签）/ <name>_sum / <name>_count
        # sum/count 与 bucket 一样带 delta>0 守卫（回绕/时钟抖动不回冲）；
        # delta 取 round 而非 int 截断——sum 的毫秒小数增量逐轮截断会系统性低估均值。
        for hist in ("request_latency_ms", "ttft_ms"):
            if sname == f"llmpod_proxy_{hist}_sum" or sname == f"llmpod_proxy_{hist}_count":
                acc = self._accum(target, f"llmpod_proxy_{hist}", labels)
                delta = self._diff(target, sname, labels, value)
                if delta > 0:
                    inc = round(delta)
                    if sname.endswith("_sum"):
                        if hist == "request_latency_ms":
                            acc.latency_sum_ms += inc
                            self._round_add(labels, latency_sum_ms=inc)
                        else:
                            acc.ttft_sum_ms += inc
                            self._round_add(labels, ttft_sum_ms=inc)
                    else:
                        if hist == "request_latency_ms":
                            acc.latency_count += inc
                            self._round_add(labels, latency_count=inc)
                        else:
                            acc.ttft_count += inc
                            self._round_add(labels, ttft_count=inc)
                return
            if sname == f"llmpod_proxy_{hist}_bucket":
                acc = self._accum(target, f"llmpod_proxy_{hist}", labels)
                delta = self._diff(target, sname, labels, value)
                idx = self._bucket_index(labels.get("le"), hist)
                if idx is not None and delta > 0:
                    buckets = acc.latency_buckets if hist == "request_latency_ms" else acc.ttft_buckets
                    # 差分的是 cumulative 桶值；rollup 存累计（查询层做插值）。
                    # 多轮采样的 cumulative 差分单调一致，直接累加即得桶内总观测数
                    buckets[idx] += int(delta)
                    # 热层留全局逐轮增量（滑动窗分位数插值的原料；不分维度——
                    # 单维度单轮样本太少，插值不稳，只在总量层出分位数）
                    hist_round = self._hist_round[0] if hist == "request_latency_ms" else self._hist_round[1]
                    hist_round[idx] += int(delta)
                return

        # counter 族：requests / rejects / retries / tokens / stream_requests。
        # rejects/requests(status=rejected) 是同一拒绝事件的两面指标——rejects
        # 按原因建行，requests(status=rejected) 与之天然重叠，只取 rejects 侧
        # 建行避免双计数；建行时补 status=rejected（rejects 样本本身不带该标签，
        # 不补则查询层无法按 status 分类出「被拒」）。
        delta = self._diff(target, sname, labels, value)
        if delta <= 0:
            return
        kind = labels.get("kind", "")
        if sname == "llmpod_proxy_rejects_total":
            acc = self._accum(target, "rejects", {**labels, "status": "rejected"})
            acc.requests += int(delta)
            self._round_add(labels, requests=int(delta), rejected=int(delta))
        elif sname == "llmpod_proxy_requests_total":
            if labels.get("status") == "rejected":
                return  # 已由 rejects_total 计数，跳过防双计
            acc = self._accum(target, "requests", labels)
            acc.requests += int(delta)
            status = labels.get("status", "")
            self._round_add(
                labels,
                requests=int(delta),
                errors=int(delta) if status == "error" else 0,
            )
        elif sname == "llmpod_proxy_retries_total":
            self._accum(target, "retries", labels).retry_count += int(delta)
        elif sname == "llmpod_proxy_tokens_total":
            acc = self._accum(target, "tokens", labels)
            if kind == "prompt":
                acc.prompt_tokens += int(delta)
                self._round_add(labels, prompt_tokens=int(delta))
            elif kind == "completion":
                acc.completion_tokens += int(delta)
                self._round_add(labels, completion_tokens=int(delta))
            elif kind == "total":
                acc.total_tokens += int(delta)
                self._round_add(labels, total_tokens=int(delta))
            elif kind == "cached":
                acc.cached_tokens += int(delta)
                self._round_add(labels, cached_tokens=int(delta))
        elif sname == "llmpod_proxy_stream_requests_total":
            self._accum(target, "stream", labels).stream_requests += int(delta)
        else:
            # 未识别的 counter 族：差分基线已建（_diff 内），但不建累计器——
            # 避免每轮写全零 rollup 行（行膨胀 + 拖慢无界查询）
            return

    def _ingest_engine_text(self, t: _EngineTarget, text: str, now: datetime) -> None:
        """引擎 /metrics 文本：白名单映射 → 快照（实时面板）+ 桶累计（落库）。

        只挑映射表内的原始指标；映射按 family 名一次解析（同族样本共享基名，
        vLLM 全量 /metrics 下逐样本映射几乎是纯开销）；解析失败按抓取失败处理
        （快照 ok=False，保留上次值；从未成功过则不建快照条目）。
        """
        self._ensure_bucket(now)
        gauges: dict[str, float] = {}
        try:
            for family in text_string_to_metric_families(text):
                mapped = _map_engine_metric(t.engine, family.name)
                if mapped is None:
                    continue
                for sample in family.samples:
                    val = _finite_or_none(sample.value)
                    if val is not None:
                        gauges[mapped] = val
        except Exception as e:
            log.warning("engine metrics parse failed (instance=%s): %r", t.instance_id, e)
            snap = self._engine_snapshots.get(t.instance_id)
            if snap is not None:
                snap.ok = False
            return

        snap = EngineSnapshot(
            instance_id=t.instance_id,
            deployment_id=t.deployment_id,
            ok=True,
            kv_cache_util=gauges.get("kv_cache_util"),
            running_requests=gauges.get("running_requests"),
            waiting_requests=gauges.get("waiting_requests"),
            prefix_cache_hit_rate=gauges.get("prefix_cache_hit_rate"),
            num_preemptions=gauges.get("num_preemptions"),
            fetched_at=now,
        )
        self._engine_snapshots[t.instance_id] = snap

        # 桶累计：gauge 记 avg/max（avg 看常态、max 看峰值），samples 区分
        # 「没变」与「没抓到」；抢占是 counter，按实例差分出桶内增量（引擎重启
        # 回绕 → 增量 = 本次值，当作从零开始）
        acc = self._engine_buckets.get(t.instance_id)
        if acc is None:
            acc = _EngineAccum(
                instance_id=t.instance_id,
                deployment_id=t.deployment_id,
                engine=t.engine,
            )
            self._engine_buckets[t.instance_id] = acc
        acc.samples += 1
        for name in _ENGINE_GAUGE_COLUMNS:
            val = gauges.get(name)
            if val is not None:
                acc.add_gauge(name, val)
        preempts = gauges.get("num_preemptions")
        if preempts is not None:
            acc.preemptions += self._diff_engine_preemption(t.instance_id, preempts)

    def _diff_engine_preemption(self, instance_id: str, value: float) -> int:
        """引擎抢占 counter 差分（回绕按从零开始口径）。"""
        last = self._engine_preempt_last.get(instance_id)
        self._engine_preempt_last[instance_id] = value
        if last is None:
            return 0
        delta = value - last if value >= last else value
        return max(0, int(round(delta)))

    def _diff(self, target: str, sname: str, labels: dict, value: float) -> float:
        """counter 差分：增量 = 本次 − 上次；回绕/重启（本次<上次）→ 增量 = 本次值。"""
        key = (target, sname, tuple(sorted(labels.items())))
        entry = self._last.get(key)
        self._last[key] = (value, time.monotonic())
        if entry is None:
            return 0.0  # 首见样本：只建基线不计增量（避免把历史存量当增量）
        last = entry[0]
        return value - last if value >= last else value

    def _bucket_index(self, le: str | None, hist: str) -> int | None:
        """`le` 标签 → 桶序号（与打点端 _LATENCY_BUCKETS 顺序对应）。"""
        from llmpod.apps.proxyserver.metrics import _LATENCY_BUCKETS

        if le is None:
            return None
        if le in ("+Inf", "inf", "Inf"):
            return len(_LATENCY_BUCKETS)
        try:
            bound = float(le)
        except ValueError:
            return None
        for i, b in enumerate(_LATENCY_BUCKETS):
            if b == bound:
                return i
        return None

    # ---- 热层（逐轮聚合 → 环形缓冲） ----

    def _round_add(self, labels: dict, **kwargs: int) -> None:
        """把一个 proxy 增量按维度记进当前轮聚合。

        维度集 = 全网关总量（__all__）+ 模型/部署/租户/API Key 四个标签维度；
        标签值为空的样本（拒绝发生在选路前、平台 Key）只进 __all__，不进维度
        构成——它们没有维度身份，混进构成面板只会多一条「未选路」噪声序列。

        不在这里做 key 上界折叠（收口时统一做）：两处各自折叠会让 ``__other__``
        在同一轮收到两个同 ts 的点（dur 双算），还可能把已有自己序列的 key
        抢折进 ``__other__``。
        """
        # get+显式建（不用 setdefault 链）：setdefault 的默认值每次调用都被急切
        # 构造再丢弃，热路径每轮白造数千个 _RoundAccum（与 _accum 同款写法）
        all_d = self._round.get("__all__")
        if all_d is None:
            all_d = self._round["__all__"] = {}
        all_acc = all_d.get("")
        if all_acc is None:
            all_acc = all_d[""] = _RoundAccum()
        all_acc.add(**kwargs)
        for dim, label in _HOT_DIM_LABELS:
            key = labels.get(label) or ""
            if not key:
                continue
            d = self._round.get(dim)
            if d is None:
                d = self._round[dim] = {}
            acc = d.get(key)
            if acc is None:
                acc = d[key] = _RoundAccum()
            acc.add(**kwargs)

    def _close_round(self, now: datetime, *, ok: bool) -> None:
        """收口一轮：冻结增量为热层点、直方图入环、重建下一轮聚合。

        全部 target 抓取失败时不产点也不推进时间基准——失败期间热层点龄
        （age_ms）必须如实变老，下一轮成功点的 dur 才能覆盖整段失败跨度，
        增量按平均口径还原。每个 key 的 dur 独立记：距「该 key」上一个点的
        跨度（含其静默期），稀疏 key 的速率不会被按全局轮间隔放大。
        """
        if not ok:
            self._round.clear()
            self._hist_round = ([0] * self._hist_len, [0] * self._hist_len)
            return
        # 总量维度每轮必产点（零流量也是数据）：在途曲线与「热层有数没数」的
        # 判断都依赖 __all__ 连续；构成维度只在有增量时出现 key
        self._round.setdefault("__all__", {}).setdefault("", _RoundAccum())
        in_flight = sum(self._in_flight_by_target.values()) if self._in_flight_by_target else 0
        # 新 key 首点的覆盖时长 = 本轮长度（增量在本轮内累积，速率分母不该按
        # 整窗或 0 算）；进程启动首轮没有时间基准，且首见样本增量为 0，dur=0
        # 无害
        round_dur = (now - self._last_round_at).total_seconds() if self._last_round_at else 0.0
        for dim, keys in self._round.items():
            dim_rings = self._hot.setdefault(dim, {})
            for key, acc in keys.items():
                ring = dim_rings.get(key)
                if ring is None:
                    # key 上界折叠收口在此统一执行（__other__ 不占席位）：满员时
                    # 新 key 顶替静默超阈值的在座 key——先到先得且永不淘汰会让
                    # 后来者永远压在 __other__ 里，真正吃流量的 key 进不了
                    # top-5；无人可替才并入 __other__，总量不丢、内存有界
                    seated = len(dim_rings) - (_HOT_OTHER_KEY in dim_rings)
                    if key != _HOT_OTHER_KEY and seated >= _HOT_MAX_KEYS:
                        victim = self._evict_hot_victim(dim_rings, now)
                        if victim is not None:
                            del dim_rings[victim]
                        else:
                            key = _HOT_OTHER_KEY
                    # __all__ 要喂环比（上一窗口），满 2h 深；构成维度只有当前
                    # 窗口读，按 1h 环深——半数内存换不来任何读者
                    maxlen = self._ring_maxlen if dim == "__all__" else self._dim_ring_maxlen
                    ring = dim_rings.setdefault(key, deque(maxlen=maxlen))
                dur = (now - ring[-1].ts).total_seconds() if ring else round_dur
                ring.append(
                    _HotPoint(
                        ts=now,
                        dur=dur,
                        requests=acc.requests,
                        errors=acc.errors,
                        rejected=acc.rejected,
                        prompt_tokens=acc.prompt_tokens,
                        completion_tokens=acc.completion_tokens,
                        total_tokens=acc.total_tokens,
                        cached_tokens=acc.cached_tokens,
                        latency_sum_ms=acc.latency_sum_ms,
                        latency_count=acc.latency_count,
                        ttft_sum_ms=acc.ttft_sum_ms,
                        ttft_count=acc.ttft_count,
                        in_flight=in_flight if dim == "__all__" else 0,
                    )
                )
        self._last_round_at = now
        if any(self._hist_round[0]) or any(self._hist_round[1]):
            self._hist_ring.append((now, list(self._hist_round[0]), list(self._hist_round[1])))
        self._round.clear()
        self._hist_round = ([0] * self._hist_len, [0] * self._hist_len)

    def _evict_hot_victim(self, dim_rings: dict, now: datetime) -> str | None:
        """席位满员时找可被顶替的在座 key：最久没点的真 key，且静默需超阈值。

        全员都在活跃期（静默 < 阈值）时返回 None——此时换座会让序列每轮反复
        清零，折进 __other__ 稳定得多；被顶替 key 的静默跨度它自己带走（期间
        本就无流量），重新活跃时按新 key 再入座。
        """
        cutoff = now - timedelta(seconds=_HOT_EVICT_IDLE)
        victim: str | None = None
        victim_ts: datetime | None = None
        for k, ring in dim_rings.items():
            if k == _HOT_OTHER_KEY or not ring:
                continue
            ts = ring[-1].ts
            if ts >= cutoff:
                continue
            if victim is None or ts < victim_ts:
                victim, victim_ts = k, ts
        return victim

    # ---- 实时面板读取口 ----

    def engine_snapshots_by_deployment(self, deployment_id: str) -> list[EngineSnapshot]:
        """某部署当前全部引擎快照（部署详情实时面板用；从未抓到的不在列）。"""
        return [s for s in self._engine_snapshots.values() if s.deployment_id == deployment_id]

    def all_engine_snapshots(self) -> list[EngineSnapshot]:
        """全部引擎快照（实时大屏整屏网格用；从未抓到的不在列）。"""
        return list(self._engine_snapshots.values())

    def hot_series(self, dim: str) -> dict[str, list[_HotPoint]]:
        """某维度全部 key 的热层序列（拷贝；实时端点降采样/构成折叠的原料）。"""
        return {key: list(ring) for key, ring in self._hot.get(dim, {}).items()}

    def hot_histograms(self) -> list[tuple[datetime, list[int], list[int]]]:
        """全局直方图逐轮增量（拷贝；滑动窗分位数插值用）。"""
        return list(self._hist_ring)

    @property
    def last_round_at(self) -> datetime | None:
        """最近一轮 proxy 抓取收口时间（热层数据年龄的基准）。"""
        return self._last_round_at

    @property
    def proxy_interval(self) -> int:
        """API 网关管道指标抓取间隔（秒；热层降采样的最小槽宽依据）。"""
        return self._proxy_interval

    # ---- 抓取链路自监控 ----

    def _record_health(self, target: str, *, ok: bool, elapsed_ms: float) -> None:
        entry = self._target_health.setdefault(target, _TargetHealth())
        entry.last_duration_ms = round(elapsed_ms, 1)
        if ok:
            entry.consecutive_failures = 0
            entry.last_success = now_local()
        else:
            entry.consecutive_failures += 1
            entry.last_failure = now_local()

    def health(self) -> dict:
        """抓取链路自检快照（/admin/dashboard/scrape-health 数据源）。

        监控链路自身坏掉时大屏只会安静地显示旧值或空值——这里把每个 target
        的最近成功时间/连续失败、待落桶积压、差分基线规模暴露出来，供值班
        判断「数据源是否还活着」。
        """

        def _target_entry(url: str) -> dict:
            h = self._target_health.get(url)
            if h is None:
                return {
                    "last_success": None,
                    "last_failure": None,
                    "consecutive_failures": None,
                    "last_duration_ms": None,
                }
            return {
                "last_success": h.last_success.isoformat() if h.last_success else None,
                "last_failure": h.last_failure.isoformat() if h.last_failure else None,
                "consecutive_failures": h.consecutive_failures,
                "last_duration_ms": h.last_duration_ms,
            }

        return {
            "intervals": {
                "proxy": self._proxy_interval,
                "engine_fast": self._engine_fast,
                "engine_slow": self._engine_slow,
                "engine_current": self._engine_interval(),
            },
            "proxy_targets": [{"target": t, **_target_entry(t)} for t in self._targets],
            "engine_targets": [
                {
                    "instance_id": iid,
                    "deployment_id": t.deployment_id,
                    "url": t.url,
                    **_target_entry(iid),  # 健康按实例记（URL 端口复用会漂移）
                }
                for iid, t in self._engine_targets.items()
            ],
            "engine_snapshots": len(self._engine_snapshots),
            "flush_pending": len(self._flush_queue),
            "engine_flush_pending": len(self._engine_flush_queue),
            "diff_baseline_keys": len(self._last),
            "hot_keys": {dim: len(keys) for dim, keys in self._hot.items()},
            "last_round_at": self._last_round_at.isoformat() if self._last_round_at else None,
        }

    # ---- 桶管理 ----

    def _ensure_bucket(self, now: datetime) -> None:
        floor = _bucket_floor(now)
        if self._bucket_start is None:
            self._bucket_start = floor
        elif floor > self._bucket_start:
            # 桶翻转：旧桶转待落队列（跨多桶 PG 故障不丢桶——间隔≥300s 或落库
            # 持续失败时可能积压多桶，flush 逐个清）。proxy 桶与引擎桶同网格
            # 翻转、各自独立入队（两张表、互不阻塞重试）。
            if self._buckets:
                self._flush_queue.append((self._bucket_start, dict(self._buckets)))
            if self._engine_buckets:
                self._engine_flush_queue.append((self._bucket_start, dict(self._engine_buckets)))
            self._bucket_start = floor
            self._buckets = {}
            self._engine_buckets = {}

    async def _maybe_flush(self, now: datetime) -> None:
        """落库全部积压桶（含保留期清理与热层修剪；所有路径必经）。

        INSERT 失败不丢桶：失败的桶留在队头下轮重试（桶内容只增，重放安全）；
        成功才出队。跨多桶故障积压时逐桶连发，直至清空。
        """
        await self._maybe_sweep(now)
        # 差分基线修剪：label 组合（api_key/model/tenant…）消失后其基线键永不再
        # 命中，长跑进程会无界泄漏；按闲置时长清（见 _prune_diff_baselines，不能
        # 按「本轮是否出现」——节拍交错与漏抓都会让键本轮缺席）
        self._prune_diff_baselines()
        self._prune_hot_idle(now)
        # 两张表各自 drain：一张落库失败不得挡另一张（引擎表队头毒数据会把
        # 引擎历史曲线静默饿死）
        await self._drain_rollup_queue()
        await self._drain_engine_queue()

    async def _drain_rollup_queue(self) -> None:
        """proxy 管道桶落库；失败保留队头下轮重试（幂等重放）。"""
        while self._flush_queue:
            bucket_start, buckets = self._flush_queue[0]
            if not buckets:
                self._flush_queue.popleft()
                continue
            rows = [
                self._to_row(target, bucket_start, acc)
                for (target, _fam, _lk), acc in buckets.items()
                if self._row_has_data(acc)
            ]
            if rows:
                try:
                    from llmpod.db import get_db_context

                    async with get_db_context() as db:
                        db.add_all(rows)
                except Exception:
                    log.exception("metric_rollup flush failed (bucket=%s rows=%d, will retry)", bucket_start, len(rows))
                    return
            self._flush_queue.popleft()

    async def _drain_engine_queue(self) -> None:
        """引擎桶落库；失败保留队头下轮重试（幂等重放）。"""
        while self._engine_flush_queue:
            bucket_start, accs = self._engine_flush_queue[0]
            rows = [
                self._engine_row(bucket_start, acc)
                for acc in accs.values()
                if acc.samples > 0  # 全桶没抓到一次的实例无信息量，不落行
            ]
            if rows:
                try:
                    from llmpod.db import get_db_context

                    async with get_db_context() as db:
                        db.add_all(rows)
                except Exception:
                    log.exception(
                        "metric_engine_rollup flush failed (bucket=%s rows=%d, will retry)",
                        bucket_start,
                        len(rows),
                    )
                    return
            self._engine_flush_queue.popleft()

    async def _maybe_sweep(self, now: datetime) -> None:
        """低频保留期清理（每小时一次顺带），proxy 桶与引擎桶一并删。

        两张表各自独立事务：任何一张失败（如库未跑新迁移、表不存在）不连坐
        回滚另一张——与 flush 队列「一张失败不挡另一张」同一口径。_last_sweep
        先推进再执行，持久性失败按小时节拍重试，不进紧密循环。
        """
        if time.monotonic() - self._last_sweep < _RETENTION_SWEEP_INTERVAL:
            return
        self._last_sweep = time.monotonic()
        cutoff = now - timedelta(days=self._cfg.metrics_retention_days)

        from llmpod.db import get_db_context
        from llmpod.models import MetricEngineRollup

        from sqlalchemy import delete

        try:
            async with get_db_context() as db:
                await db.execute(delete(MetricRollup).where(MetricRollup.bucket_start < cutoff))
        except Exception:
            log.exception("metric_rollup retention sweep failed")
        try:
            async with get_db_context() as db:
                await db.execute(delete(MetricEngineRollup).where(MetricEngineRollup.bucket_start < cutoff))
        except Exception:
            log.exception("metric_engine_rollup retention sweep failed")

    def _accum(self, target: str, fam: str, labels: dict) -> _Accum:
        # `le` 只标识直方图桶序，不进累计器 key（同一样本族必须聚合到一条累计器）
        key_labels = {k: v for k, v in labels.items() if k != "le"}
        key = (target, fam, tuple(sorted(key_labels.items())))
        acc = self._buckets.get(key)
        if acc is None:
            acc = _Accum(labels=key_labels)
            self._buckets[key] = acc
        return acc

    def _prune_diff_baselines(self) -> None:
        """清掉闲置超阈值的差分基线键（防 label 组合 churn 导致的内存无界增长）。

        存活性看「距上次出现的时长」——proxy 轮缺席（节拍交错/单 target 漏抓）
        不算消失，只有长时间不再出现才清；清后重现等价一次 counter 重启（首见
        只建基线），语义安全。
        """
        cutoff = time.monotonic() - _DIFF_BASELINE_IDLE_SECONDS
        stale = [key for key, (_val, seen_at) in self._last.items() if seen_at < cutoff]
        for key in stale:
            del self._last[key]

    def _prune_hot_idle(self, now: datetime) -> None:
        """清掉闲置超过阈值的热层 key（低频流量模型/已下线部署不占内存）。"""
        cutoff = now - timedelta(seconds=_HOT_IDLE_SECONDS)
        for dim in list(self._hot):
            keys = self._hot[dim]
            for key in list(keys):
                ring = keys[key]
                if ring and ring[-1].ts < cutoff:
                    del keys[key]
            if not keys:
                del self._hot[dim]

    def _buckets_for_target(self, list_target: str) -> list[_Accum]:
        return [a for (t, _f, _k), a in self._buckets.items() if t == list_target]

    @staticmethod
    def _row_has_data(acc: _Accum) -> bool:
        """累计器是否含任何有效数值——全零行不落库（首见样本只建差分基线时会
        产生空累计器），避免 rollup 被无信息行膨胀、放大查询成本。"""
        return bool(
            acc.requests
            or acc.prompt_tokens
            or acc.completion_tokens
            or acc.total_tokens
            or acc.cached_tokens
            or acc.retry_count
            or acc.stream_requests
            or acc.latency_count
            or acc.ttft_count
            or acc.in_flight_peak
            or any(acc.latency_buckets)
            or any(acc.ttft_buckets)
        )

    def _to_row(self, target: str, bucket_start: datetime, acc: _Accum) -> MetricRollup:
        """攒桶累计值 → MetricRollup ORM 行（标签列从 labels 投影）。"""
        labels = acc.labels
        return MetricRollup(
            bucket_start=bucket_start,
            source=target,
            tenant_id=_to_int(labels.get("tenant_id", "")),
            api_key_id=_to_int(labels.get("api_key_id", "")),
            model_alias=labels.get("model_alias") or None,
            endpoint_id=_to_int(labels.get("endpoint_id", "")),
            deployment_id=labels.get("deployment_id") or None,
            engine=labels.get("engine") or None,
            operation=labels.get("operation") or None,
            upstream_type=labels.get("upstream_type") or None,
            status=labels.get("status") or None,
            reject_reason=labels.get("reject_reason") or None,
            requests=acc.requests,
            prompt_tokens=acc.prompt_tokens,
            completion_tokens=acc.completion_tokens,
            total_tokens=acc.total_tokens,
            cached_tokens=acc.cached_tokens,
            retry_count=acc.retry_count,
            stream_requests=acc.stream_requests,
            latency_sum_ms=acc.latency_sum_ms,
            latency_count=acc.latency_count,
            latency_buckets=acc.latency_buckets,
            ttft_sum_ms=acc.ttft_sum_ms,
            ttft_count=acc.ttft_count,
            ttft_buckets=acc.ttft_buckets,
            in_flight_peak=acc.in_flight_peak,
        )

    def _engine_row(self, bucket_start: datetime, acc: _EngineAccum):
        """引擎桶累计 → MetricEngineRollup ORM 行（gauge avg/max 成对）。"""
        from llmpod.models import MetricEngineRollup

        return MetricEngineRollup(
            bucket_start=bucket_start,
            instance_id=acc.instance_id,
            deployment_id=acc.deployment_id,
            engine=acc.engine or None,
            preemptions=acc.preemptions,
            samples=acc.samples,
            kv_cache_util_avg=acc.avg("kv_cache_util"),
            kv_cache_util_max=acc.max_of("kv_cache_util"),
            running_requests_avg=acc.avg("running_requests"),
            running_requests_max=acc.max_of("running_requests"),
            waiting_requests_avg=acc.avg("waiting_requests"),
            waiting_requests_max=acc.max_of("waiting_requests"),
            prefix_cache_hit_rate_avg=acc.avg("prefix_cache_hit_rate"),
            prefix_cache_hit_rate_max=acc.max_of("prefix_cache_hit_rate"),
        )


# ---- lifespan 接线（照 upload sweep 的 start/stop 模式） ----

# 进程内单例（webserver 与 routes/admin 共进程；实时面板直读快照的唯一入口）
_scraper: MetricsScraper | None = None


def get_scraper() -> MetricsScraper | None:
    """获取当前 scraper 单例（未启动/单测环境返回 None，调用方自行兜底）。

    注意不懒创建：返回 None 而非 new 一个——shutdown 窗口里请求若触发懒创建，
    会拿到一个从未抓取的空 scraper，面板显示「全部引擎下线」的假信号。
    """
    return _scraper


def touch_scraper_demand() -> None:
    """路由侧触碰引擎抓取需求信号（消费即触碰；scraper 未启动时静默跳过）。"""
    scraper = get_scraper()
    if scraper is not None:
        scraper.touch_demand()


async def start_metrics_scraper_task(background_tasks: BackgroundTaskManager, cfg: Settings | None = None) -> None:
    """启动 scraper 后台监督任务（control_plane_lifespan 调）。

    PG 未初始化（构造失败）时降级记 warning、不注册。模块单例 ``_scraper`` 的发布与
    清理由任务自身持有：进入 ``run()`` 前发布、退出（含被 cancel）时收回——路由侧
    不会在关停窗口拿到已死的 scraper。
    """
    from llmpod.config.settings import settings as global_settings

    cfg = cfg or global_settings
    try:
        scraper = MetricsScraper(cfg)
    except Exception as e:
        log.warning("metrics scraper disabled: %s", e)
        return

    async def _run() -> None:
        global _scraper
        _scraper = scraper
        try:
            await scraper.run()
        finally:
            _scraper = None

    background_tasks.start(_run, name="metrics-scraper")
    log.info(
        "metrics scraper started (proxy=%ss engine=%s/%ss static_targets=%d)",
        scraper._proxy_interval,
        scraper._engine_fast,
        scraper._engine_slow,
        len(scraper._targets),  # 同模块内读自身私有属性
    )
