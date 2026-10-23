import { computed, ref } from 'vue'
import {
  getDashboardModels,
  getDashboardRealtime,
  getDashboardTokens,
  type RealtimeData,
  type RealtimeDim,
  type RealtimeHistData,
  type RealtimeKpiKey,
  type RealtimeLenHistData,
  type RealtimeParams,
} from '@/api/dashboard'
import {
  buildKpiCard,
  compareLabel,
  diffCumulative,
  EMPTY_TEXT,
  formatCompact,
  formatGrouped,
  formatLatency,
  formatRate,
  type CustomRange,
  type KpiCard,
  type KpiCardRaw,
  type KpiCardSpec,
  formatSlotLabel,
  formatTokensFull,
  histLabels,
  maxOf,
  tokenBoundLabel,
  type WindowValue,
  windowToRange,
  windowToRealtimeQuery,
} from '@/views/dashboard-shared/format'

/** 模型统计宽表行(含长尾折叠行与合计行) */
export interface ModelRow {
  model: string
  calls: string
  share: string
  latency: string
  error: string
  truncation: string
  input: string
  output: string
  cached: string
  total: string
  isTotal: boolean
}

/** 堆叠调用趋势视图模型(总量与构成在同一槽网格上对齐) */
export interface StackedView {
  labels: string[]
  series: Array<{ key: string; values: number[] }>
  requests: number[]
  latencyMs: Array<number | null>
  /** y 轴计数轴单位按组内最大值(§1.5) */
  axisMax: number
}

/** 单张分布视图模型(累计桶已差分;x 轴标签在顶层 labels,bars 不再重复携带) */
export interface DistView {
  labels: string[]
  bars: Array<{ count: number; shareText: string }>
  avgText: string | null
  subText: string | null
}

// ---- 模块级状态(单例) ----

/** 当前窗口词表(全屏唯一窗口参数,§6 屏2 #4;请求时换算 from/to + prev_*;custom 走自定义区间) */
const activeWindow = ref<WindowValue>('today')

/** 自定义日期区间(仅 'custom' 窗口生效;毫秒元组直配页头 n-date-picker) */
const customRange = ref<CustomRange | null>(null)

/** 整屏筛选(§3:作用于所有代理侧面板;引擎网格不参与,屏2 无引擎面板) */
const filterModel = ref('')
const filterTenantId = ref<number | null>(null)
const filterApiKey = ref<number | null>(null)

const statCards = ref<KpiCard[]>([])
const stacked = ref<StackedView | null>(null)
const distributions = ref<{
  latency: DistView | null
  ttft: DistView | null
  promptLen: DistView | null
  generationLen: DistView | null
}>({ latency: null, ttft: null, promptLen: null, generationLen: null })
const modelRows = ref<ModelRow[]>([])
const sourceText = ref('')

/** 当前窗口的环比文案(null = 多日窗,隐藏环比区) */
const compareText = computed(() => compareLabel(activeWindow.value))

/** 整屏请求参数(窗口全屏唯一;§3 三个筛选下推,dim 固定 model;2026-09-17 区间化:窗口换算 from/to + prev_*,不再传 window) */
function realtimeParams(now: Date): RealtimeParams {
  return {
    ...windowToRealtimeQuery(activeWindow.value, now, customRange.value),
    dim: 'model',
    ...(filterModel.value ? { model: filterModel.value } : {}),
    ...(filterTenantId.value !== null ? { tenant_id: filterTenantId.value } : {}),
    ...(filterApiKey.value !== null ? { api_key_id: filterApiKey.value } : {}),
  }
}

/** KPI 六卡配置(§7.4;成功率为派生指标:1 − error_rate,delta_pp 取反) */
const KPI_SPECS: Array<KpiCardSpec & { key: RealtimeKpiKey | 'success_rate' }> = [
  { key: 'calls', label: '调用量', kind: 'pct', badDirection: 'down', format: value => formatCompact(value) },
  { key: 'success_rate', label: '成功率', kind: 'pp', badDirection: 'down', format: value => formatRate(value) },
  { key: 'latency_avg_ms', label: '平均延迟', kind: 'ms', badDirection: 'up', format: value => formatLatency(value) },
  { key: 'total_tokens_kpi', label: '总 Token', kind: 'pct', badDirection: 'down', format: value => formatCompact(value) },
  { key: 'avg_context', label: '平均上下文', kind: 'abs', badDirection: 'neutral', format: value => formatCompact(value) },
  { key: 'truncation_rate', label: '截断率', kind: 'pp', badDirection: 'up', format: value => formatRate(value) },
]

/** KPI 六卡视图:success_rate 为派生键(1 − error_rate),多日窗抹掉环比(§5) */
function toStatCards(data: RealtimeData, suffix: string | null): KpiCard[] {
  const error = data.kpi.error_rate
  // 派生成功率:值取反、delta_pp 取反(方向语义随之反转,配色仍按「成功率降=坏」)
  const derived: KpiCardRaw = {
    ...error,
    value: error.value === null ? null : 1 - error.value,
    delta_pp: error.delta_pp === null ? null : -error.delta_pp,
  }
  const rawByKey = (key: RealtimeKpiKey | 'success_rate'): KpiCardRaw | undefined => {
    const raw = key === 'success_rate' ? derived : data.kpi[key]
    // 多日窗无环比(§5):suffix 为 null 时 delta 一并抹掉(整个环比区隐藏),而非仅去掉后缀
    return suffix === null && raw
      ? { ...raw, delta_pct: null, delta: null, delta_pp: null, delta_ms: null }
      : raw
  }
  return KPI_SPECS.map(spec => buildKpiCard(spec, rawByKey(spec.key), suffix ? ` ${suffix}` : ''))
}

/** 堆叠调用趋势:总量 trend.points 与构成 composition.items 同槽网格(§6 屏2 #3) */
function toStacked(data: RealtimeData): StackedView | null {
  // 空窗(无时间槽或无构成)→ null:趋势图走 emptyChartOption 骨架——
  // 空数组的 value 轴不生成刻度区间,splitLine 一条不画会整块空白
  if (!data.trend.points.length || !data.composition.items.length) return null
  const granularity = data.window.granularity
  const points = data.trend.points
  const labels = points.map(point => formatSlotLabel(point.ts, granularity))
  const requests = points.map(point => point.requests)
  const series = data.composition.items.map(item => ({
    key: item.key,
    values: points.map((point) => {
      // 构成序列与总量同下标同 ts 对齐;末位兜底防轻微错位
      const matched = item.requests.find(slot => slot.ts === point.ts)
      return matched ? matched.value : 0
    }),
  }))
  return {
    labels,
    series,
    requests,
    latencyMs: points.map(point => point.latency_avg_ms),
    axisMax: maxOf(requests),
  }
}

/** 延迟/首字分布(累计桶差分,§3.2) */
function toDist(hist: RealtimeHistData | null): DistView | null {
  if (!hist) return null
  const counts = diffCumulative(hist.buckets)
  const total = hist.buckets[hist.buckets.length - 1] ?? 0
  const labels = histLabels(hist.bounds, formatLatency)
  return {
    labels,
    bars: counts.map(count => ({
      count,
      shareText: formatRate(total === 0 ? 0 : count / total, 1),
    })),
    avgText: hist.avg_ms === null ? null : formatLatency(hist.avg_ms),
    subText: hist.slow_ratio === null ? null : `慢请求 ${formatRate(hist.slow_ratio, 1)}`,
  }
}

/** 输入/输出长度分布(token 桶) */
function toLenDist(hist: RealtimeLenHistData | null): DistView | null {
  if (!hist) return null
  const counts = diffCumulative(hist.buckets)
  const total = hist.buckets[hist.buckets.length - 1] ?? 0
  const labels = histLabels(hist.bounds, tokenBoundLabel)
  return {
    labels,
    bars: counts.map(count => ({
      count,
      shareText: formatRate(total === 0 ? 0 : count / total, 1),
    })),
    avgText: hist.avg_tokens === null ? null : `${hist.avg_tokens.toLocaleString()} tokens`,
    subText: null,
  }
}

/** 数据源徽标(§3.0:hot 标秒龄,cold 标 5 分钟桶) */
function toSourceText(window: RealtimeData['window']): string {
  if (window.source === 'hot') {
    const age = window.age_ms === null ? '' : ` · ${Math.max(1, Math.round(window.age_ms / 1000))}s 前`
    return `实时${age}`
  }
  return '5 分钟桶'
}

/**
 * 屏2 数据 store:/admin/dashboard/realtime 整屏 + /models + /tokens 表格。
 * 「一屏一次往返」:代理侧面板全部来自 realtime;表格随窗口/筛选变化(不轮询)。
 */
export function useStore() {
  async function fetchRealtime(signal?: AbortSignal, now = new Date()): Promise<RealtimeData> {
    const data = await getDashboardRealtime(realtimeParams(now), signal)
    statCards.value = toStatCards(data, compareLabel(activeWindow.value))
    stacked.value = toStacked(data)
    distributions.value = {
      latency: toDist(data.distribution?.latency ?? null),
      ttft: toDist(data.distribution?.ttft ?? null),
      promptLen: toLenDist(data.distribution?.prompt_len ?? null),
      generationLen: toLenDist(data.distribution?.generation_len ?? null),
    }
    sourceText.value = toSourceText(data.window)
    return data
  }

  async function fetchTables(signal?: AbortSignal, now = new Date()) {
    const range = windowToRange(activeWindow.value, now, customRange.value)
    const modelsParams = {
      ...range,
      top_n: 10,
      ...(filterModel.value ? { model: filterModel.value } : {}),
      ...(filterTenantId.value !== null ? { tenant_id: filterTenantId.value } : {}),
    }
    const tokensParams = {
      dim: 'model' as RealtimeDim,
      ...range,
      top: 10,
      ...(filterModel.value ? { model: filterModel.value } : {}),
      ...(filterTenantId.value !== null ? { tenant_id: filterTenantId.value } : {}),
    }
    const [models, tokens] = await Promise.all([
      getDashboardModels(modelsParams, signal),
      getDashboardTokens(tokensParams, signal),
    ])

    const tokenByKey = new Map(tokens.rows.map(row => [row.key, row]))
    const callsMax = maxOf(models.map(row => row.call_count))
    const totalMax = maxOf([
      ...tokens.rows.map(row => row.total_tokens),
      tokens.other?.total_tokens ?? 0,
      tokens.total.total_tokens,
    ])

    const rows: ModelRow[] = models.map((row) => {
      const token = tokenByKey.get(row.model ?? '')
      return {
        model: row.model ?? EMPTY_TEXT,
        calls: formatGrouped(row.call_count, callsMax),
        share: formatRate(row.ratio, 1),
        latency: row.avg_latency_ms === null ? EMPTY_TEXT : formatLatency(row.avg_latency_ms),
        error: formatRate(row.error_rate),
        truncation: row.truncation_rate === null ? EMPTY_TEXT : formatRate(row.truncation_rate),
        input: token ? formatTokensFull(token.input_tokens) : EMPTY_TEXT,
        output: token ? formatTokensFull(token.output_tokens) : EMPTY_TEXT,
        cached: token ? formatTokensFull(token.cached_tokens) : EMPTY_TEXT,
        total: token ? formatGrouped(token.total_tokens, totalMax) : EMPTY_TEXT,
        isTotal: false,
      }
    })

    if (tokens.other) {
      rows.push({
        model: tokens.other.key,
        calls: EMPTY_TEXT,
        share: formatRate(tokens.other.ratio, 1),
        latency: EMPTY_TEXT,
        error: EMPTY_TEXT,
        truncation: EMPTY_TEXT,
        input: formatTokensFull(tokens.other.input_tokens),
        output: formatTokensFull(tokens.other.output_tokens),
        cached: formatTokensFull(tokens.other.cached_tokens),
        total: formatGrouped(tokens.other.total_tokens, totalMax),
        isTotal: false,
      })
    }

    return { rows, tokens, totalMax }
  }

  /** 整屏拉取:realtime + 表格并发,三路共用同一 now(同轮 from/to 基准一致);合计行依赖 realtime 窗口级 kpi,在两者到齐后拼装 */
  async function fetchAll(signal?: AbortSignal) {
    const now = new Date()
    const [realtime, tables] = await Promise.all([fetchRealtime(signal, now), fetchTables(signal, now)])
    const { rows, tokens, totalMax } = tables

    // 调用量列为两段式格式:/models 行组=top10 行内最大,合计行组=窗口总量自身;
    // 仅当 top10 全部 <10万而窗口合计 ≥10万时列内会千分位与 K 混排,属可接受近似(§1.5)。
    rows.push({
      model: '合计',
      calls: realtime.kpi.calls.value === null ? EMPTY_TEXT : formatCompact(realtime.kpi.calls.value),
      share: formatRate(
        tokens.rows.reduce((sum, row) => sum + row.ratio, 0) + (tokens.other?.ratio ?? 0),
        1,
      ),
      latency: EMPTY_TEXT,
      error: realtime.kpi.error_rate.value === null ? EMPTY_TEXT : formatRate(realtime.kpi.error_rate.value),
      truncation:
        realtime.kpi.truncation_rate.value === null
          ? EMPTY_TEXT
          : formatRate(realtime.kpi.truncation_rate.value),
      input: formatTokensFull(tokens.total.input_tokens),
      output: formatTokensFull(tokens.total.output_tokens),
      total: formatGrouped(tokens.total.total_tokens, totalMax),
      cached: formatTokensFull(tokens.total.cached_tokens),
      isTotal: true,
    })
    modelRows.value = rows
  }

  return {
    activeWindow,
    customRange,
    filterModel,
    filterTenantId,
    filterApiKey,
    statCards,
    stacked,
    distributions,
    modelRows,
    sourceText,
    compareText,
    fetchAll,
  }
}
