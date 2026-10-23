import { ref } from 'vue'
import {
  getDashboardAnomaly,
  getDashboardEngineHistory,
  getDashboardRealtime,
  type AnomalyData,
  type RealtimeData,
  type RealtimeDim,
  type RealtimeEngineData,
  type RealtimeKpiKey,
} from '@/api/dashboard'
import {
  buildKpiCard,
  compareLabel,
  type CustomRange,
  EMPTY_TEXT,
  formatCompact,
  formatLatency,
  formatRate,
  formatSlotLabel,
  type WindowValue,
  windowToRealtimeQuery,
  type KpiCard,
  type KpiCardRaw,
  type KpiCardSpec,
} from '@/views/dashboard-shared/format'

/** KV 饱和关注阈值(§8 #9:阈值告警后端未做,前端先定) */
export const KV_SATURATION_RATE = 0.9

/** 外部上游需关注阈值 */
export const UPSTREAM_ATTENTION_RATE = 0.9

/** 成功率健康阈值(≥99% 视为健康) */
export const UPSTREAM_HEALTHY_RATE = 0.99

/** 成功率预警阈值(90–95% 预警,95–99% 视为正常抖动) */
export const UPSTREAM_WARNING_RATE = 0.95

/** 成功率文字色分档:≥99% 绿、95–99% 中性(不着色)、90–95% 橙、<90% 红(与 UPSTREAM_ATTENTION_RATE 同界);无数据不着色 */
export function successRateTextClass(rate: number | null): string {
  if (rate === null) return ''
  if (rate >= UPSTREAM_HEALTHY_RATE) return 'text-success-text'
  if (rate >= UPSTREAM_WARNING_RATE) return ''
  if (rate >= UPSTREAM_ATTENTION_RATE) return 'text-warning-text'
  return 'text-error-text'
}

/** 引擎网格行 */
export interface EngineRow {
  id: string
  name: string
  rank: number
  engine: string
  status: string
  /** false = 最近一轮抓取失败(llama.cpp 恒 false),指标列全 — */
  ok: boolean
  /** 0-1,供进度条;null = 不可用 */
  kvPct: number | null
  kvText: string
  waiting: string
  running: string
  hitText: string
  preemptions: number
  preemptionAlert: boolean
  ageText: string
}

/** 吞吐趋势(近 1h 默认;qpm/tpm 支持基准化索引显示) */
export interface ThroughputView {
  labels: string[]
  qpm: number[]
  tpm: number[]
  canIndex: boolean
  baselineQpm: number | null
  baselineTpm: number | null
}

/** 热力图单元格(缺桶为 null,渲染空档) */
export interface HeatCell {
  kvAvg: number
  kvMax: number
  preemptions: number
  samples: number
}

export interface HeatmapView {
  labels: string[]
  rows: Array<{ label: string; cells: Array<HeatCell | null> }>
}

/** 异常构成条目 */
export interface AnomalyItemView {
  label: string
  count: string
  shareText: string
  sharePct: number
  /** 归属原始值(rejected=准入拒绝 / error=上游错误),供占比条着色 */
  side: string
  sideText: string
}

export interface AnomalyView {
  totalText: string
  /** 占窗口总请求比例文本;可能 >100%,只做文本不做进度条(§4.2) */
  ratioText: string
  items: AnomalyItemView[]
}

/** 上游健康条目 */
export interface UpstreamHealthItem {
  name: string
  successText: string
  /** 原始成功率 0-1(供列表分档着色);null = 无样本 */
  successRate: number | null
  latencyText: string
  qpmText: string
  attention: boolean
}

export interface UpstreamHealth {
  local: UpstreamHealthItem | null
  external: UpstreamHealthItem | null
  providers: UpstreamHealthItem[]
}

export interface BannerView {
  ok: boolean
  items: string[]
}

/** 异常 kind → 中文标签(§8 #3:三类汇总映射由前端定义,未知 kind 原样) */
const ANOMALY_KIND_LABELS: Record<string, string> = {
  upstream_timeout: '上游超时',
  upstream_5xx: '上游 5xx',
  upstream_4xx: '上游 4xx',
  upstream_connect: '上游连接失败',
  rate_limited: '限流命中',
  context_length: '上下文超长',
  quota_exhausted: '额度耗尽',
  other: '其他',
}

const SIDE_LABELS: Record<string, string> = {
  rejected: '准入拒绝',
  error: '上游错误',
}

/** 异常构成占比条色:准入拒绝橙、上游错误红(其余未知值按上游错误兜底);--n-* 是 RGB 串,需 rgb() 包裹 */
export function anomalyBarColor(side: string): string {
  return side === 'rejected' ? 'rgb(var(--n-warning-color))' : 'rgb(var(--n-error-color))'
}

// ---- 模块级状态(单例) ----

/** 全屏唯一窗口(KV 热力图/异常/上游健康同用;请求时换算 from/to,/realtime 另带 prev_*;custom 走自定义区间) */
const realtimeWindow = ref<WindowValue>('1h')

/** 自定义日期区间(仅 'custom' 窗口生效;毫秒元组直配页头 n-date-picker) */
const customRange = ref<CustomRange | null>(null)

const kpiCards = ref<KpiCard[]>([])
const engineRows = ref<EngineRow[]>([])
const throughput = ref<ThroughputView | null>(null)
const heatmap = ref<HeatmapView | null>(null)
const anomalyView = ref<AnomalyView | null>(null)
const upstreamHealth = ref<UpstreamHealth | null>(null)
const banner = ref<BannerView>({ ok: true, items: [] })
const tsText = ref('')

/** 横幅跨两路数据(KV 饱和来自 realtime、上游健康来自 anomaly):模块级快照与单例 refs 生命周期对齐,路由重进后仍保留 last-good */
let lastEngines: RealtimeEngineData[] = []
let lastAnomalyUpstream: AnomalyData['upstream'] | null = null

/** KPI 八卡(§7.5:前 4 张「还吃得下吗」、后 4 张「有没有在坏」) */
function toKpiCards(data: RealtimeData): KpiCard[] {
  const engines = data.engines
  const busyRank = (raw: KpiCardRaw): string | null => {
    const instanceId = raw.instance_id ?? null
    if (!instanceId) return null
    const instance = engines.find(engine => engine.instance_id === instanceId)
    return instance ? `最忙 ${instance.deployment_name ?? instance.deployment_id} #${instance.rank}` : null
  }
  const specs: Array<KpiCardSpec & { key: RealtimeKpiKey }> = [
    {
      key: 'in_flight',
      label: '在途峰值',
      kind: 'pct',
      badDirection: 'up',
      // 在途是网关口径瞬时值,冷层双 null → —(§5)
      format: (value, raw) => (raw.peak === null || raw.peak === undefined ? String(value) : `${value} / ${raw.peak}`),
    },
    {
      key: 'kv_cache_util',
      label: 'KV 水位',
      kind: null,
      badDirection: 'up',
      format: value => formatRate(value, 1),
      note: raw => busyRank(raw),
    },
    {
      key: 'waiting_requests',
      label: '排队',
      kind: null,
      badDirection: 'up',
      format: value => String(value),
    },
    {
      key: 'avg_context',
      label: '平均上下文',
      kind: 'abs',
      badDirection: 'neutral',
      format: value => formatCompact(value),
    },
    {
      key: 'error_rate',
      label: '错误率',
      kind: 'pp',
      badDirection: 'up',
      format: value => formatRate(value),
    },
    {
      key: 'ttft_avg_ms',
      label: '首字延迟',
      kind: 'ms',
      badDirection: 'up',
      format: value => formatLatency(value),
    },
    {
      key: 'latency_avg_ms',
      label: '平均延迟',
      kind: 'ms',
      badDirection: 'up',
      format: value => formatLatency(value),
    },
    {
      key: 'cache_hit_rate',
      label: '缓存命中率',
      kind: 'pp',
      badDirection: 'down',
      format: value => formatRate(value),
    },
  ]
  const suffix = compareLabel(realtimeWindow.value)
  return specs.map(spec =>
    buildKpiCard(
      spec,
      data.kpi[spec.key],
      suffix ? ` ${suffix}` : '',
    ),
  )
}

function toEngineRows(engines: RealtimeEngineData[]): EngineRow[] {
  return engines.map(engine => ({
    id: engine.instance_id,
    name: engine.deployment_name ?? engine.deployment_id,
    rank: engine.rank,
    engine: engine.engine ?? '—',
    status: engine.status,
    ok: engine.ok,
    kvPct: engine.kv_cache_util,
    kvText: engine.kv_cache_util === null ? EMPTY_TEXT : formatRate(engine.kv_cache_util, 0),
    waiting: engine.waiting_requests === null ? EMPTY_TEXT : String(engine.waiting_requests),
    running: engine.running_requests === null ? EMPTY_TEXT : String(engine.running_requests),
    hitText: engine.prefix_cache_hit_rate === null ? EMPTY_TEXT : formatRate(engine.prefix_cache_hit_rate),
    preemptions: engine.num_preemptions ?? 0,
    preemptionAlert: (engine.num_preemptions ?? 0) > 0,
    ageText: engine.age_ms === null ? EMPTY_TEXT : `${Math.max(1, Math.round(engine.age_ms / 1000))}s 前`,
  }))
}

/** 关注横幅(§7.5 顶栏):KV 饱和实例 + 低成功率外部上游/供应商 */
function toBanner(engines: RealtimeEngineData[], upstream: AnomalyData['upstream'] | null): BannerView {
  const items: string[] = []
  for (const engine of engines) {
    if (engine.kv_cache_util !== null && engine.kv_cache_util > KV_SATURATION_RATE) {
      const name = engine.deployment_name ?? engine.deployment_id
      items.push(`${name} #${engine.rank} KV ${formatRate(engine.kv_cache_util, 0)}(接近饱和)`)
    }
  }
  const external = upstream?.external
  if (external && external.success_rate !== null && external.success_rate < UPSTREAM_ATTENTION_RATE)
    items.push(`外部上游成功率 ${formatRate(external.success_rate)}`)
  for (const provider of upstream?.providers ?? []) {
    if (provider.success_rate !== null && provider.success_rate < UPSTREAM_ATTENTION_RATE)
      items.push(`${provider.name} 错误率 ${formatRate(1 - provider.success_rate, 1)}`)
  }
  return { ok: items.length === 0, items }
}

function toUpstreamHealth(data: AnomalyData): UpstreamHealth {
  const toCard = (name: string, card: { success_rate: number | null; latency_avg_ms: number | null; qpm: number } | null): UpstreamHealthItem | null =>
    card
      ? {
          name,
          successText: card.success_rate === null ? EMPTY_TEXT : formatRate(card.success_rate),
          successRate: card.success_rate,
          latencyText: card.latency_avg_ms === null ? EMPTY_TEXT : formatLatency(card.latency_avg_ms),
          qpmText: formatCompact(card.qpm),
          attention: card.success_rate !== null && card.success_rate < UPSTREAM_ATTENTION_RATE,
        }
      : null
  return {
    local: toCard('local', data.upstream.local),
    external: toCard('external', data.upstream.external),
    providers: data.upstream.providers.map(provider => toCard(provider.name, provider)!),
  }
}

/** 屏3 数据 store:三路独立轮询(/realtime 10s、/engines/history 60s、/anomaly 30s,节奏由 controller 驱动) */
export function useStore() {
  async function fetchRealtime(signal?: AbortSignal) {
    // 2026-09-17 区间化:窗口词表换算 from/to + prev_*(多日窗/自定义不传 prev_*),不再传 window
    const params = { ...windowToRealtimeQuery(realtimeWindow.value, undefined, customRange.value), dim: 'model' as RealtimeDim }
    const data = await getDashboardRealtime(params, signal)
    lastEngines = data.engines

    kpiCards.value = toKpiCards(data)
    engineRows.value = toEngineRows(data.engines)

    const baseline = data.trend.baseline
    throughput.value = {
      labels: data.trend.points.map(point => formatSlotLabel(point.ts, data.window.granularity)),
      qpm: data.trend.points.map(point => point.qpm),
      tpm: data.trend.points.map(point => point.tpm),
      canIndex: (baseline.qpm ?? 0) > 0 && (baseline.tpm ?? 0) > 0,
      baselineQpm: baseline.qpm ?? null,
      baselineTpm: baseline.tpm ?? null,
    }

    // 横幅含上游维度(anomaly 一路),用最近一次数据重算;fetchAnomaly 后再算一次
    banner.value = toBanner(data.engines, lastAnomalyUpstream)
    tsText.value = data.ts
  }

  async function fetchHistory(signal?: AbortSignal) {
    // 与 /realtime 同一窗口的区间(不带 prev_*;滚动窗随轮询时间自然前移)
    const { from, to } = windowToRealtimeQuery(realtimeWindow.value, undefined, customRange.value)
    const data = await getDashboardEngineHistory({ from, to }, signal)

    // 格并集排序为列;行按实例;缺格 → null 空档(不补 0,§4.1)。列标签按后端返回的粒度(5m/1h/1d)切换格式
    const granularity = data.window.granularity
    const tsList = Array.from(
      new Set(data.instances.flatMap(instance => instance.series.map(point => point.ts))),
    ).sort((a, b) => Date.parse(a) - Date.parse(b))
    heatmap.value = {
      labels: tsList.map(ts => formatSlotLabel(ts, granularity)),
      rows: data.instances.map(instance => ({
        label: `${instance.deployment_name ?? instance.deployment_id} #${instance.rank}`,
        cells: tsList.map(ts => {
          const point = instance.series.find(item => item.ts === ts)
          return point && point.kv_avg !== null
            ? { kvAvg: point.kv_avg, kvMax: point.kv_max ?? 0, preemptions: point.preemptions, samples: point.samples } // avg 在而 max 缺时按 0 兜底(Task 10 画 max 边框时留意)
            : null
        }),
      })),
    }
  }

  async function fetchAnomaly(signal?: AbortSignal) {
    const { from, to } = windowToRealtimeQuery(realtimeWindow.value, undefined, customRange.value)
    const data = await getDashboardAnomaly({ from, to }, signal)
    lastAnomalyUpstream = data.upstream

    anomalyView.value = {
      totalText: data.anomaly.total.toLocaleString(),
      ratioText: formatRate(data.anomaly.ratio, 1),
      items: data.anomaly.items.map(item => ({
        label: ANOMALY_KIND_LABELS[item.kind] ?? item.kind,
        count: item.count.toLocaleString(),
        shareText: formatRate(item.share, 1),
        sharePct: Math.round(item.share * 1000) / 10,
        side: item.side,
        sideText: SIDE_LABELS[item.side] ?? item.side,
      })),
    }
    upstreamHealth.value = toUpstreamHealth(data)

    // KV 部分沿用最近一次 realtime 的 engines 快照
    banner.value = toBanner(lastEngines, data.upstream)
  }

  return {
    realtimeWindow,
    customRange,
    kpiCards,
    engineRows,
    throughput,
    heatmap,
    anomalyView,
    upstreamHealth,
    banner,
    tsText,
    fetchRealtime,
    fetchHistory,
    fetchAnomaly,
  }
}
