import type { OverviewTrendPointData, RealtimeWindow } from '@/api/dashboard'

/** 大屏空值文案:null = 无样本,不是 0(对接文档 §1.1) */
export const EMPTY_TEXT = '—'

/** 构成图长尾折叠 key(后端固定值) */
export const OTHER_KEY = '__other__'

/** 构成长尾固定灰,不参与调色 */
export const OTHER_COLOR = '#9aa3b2'

/**
 * 构成图数据色板:数据可视化序列色(非 UI 主题色——UI 颜色仍走 UnoCSS token 与 --n-*、--c-* 变量,
 * 主题联动由图表轴色 cssVar/cssRgb 负责,序列色固定以保证「颜色跟 key 走」)。
 */
export const SERIES_PALETTE = [
  '#5b8def', '#34c38f', '#f1b44c', '#f46a6a', '#7c61d6',
  '#29b8c4', '#eb6a9f', '#8ac34a', '#e08a4f', '#6d7fde',
] as const

export function trimZero(text: string): string {
  if (!text.includes('.')) return text
  return text.replace(/0+$/, '').replace(/\.$/, '')
}

/** 组内最大值(定单位用) */
export function maxOf(values: number[]): number {
  return values.length ? Math.max(...values) : 0
}

/** §1.5 数字单位:按「这一组数据的最大值」定单位,同一张图/同一列单位统一 */
export function formatGrouped(value: number, groupMax: number): string {
  if (groupMax >= 10_000_000) return `${trimZero((value / 1_000_000).toFixed(1))}M`
  if (groupMax >= 100_000) return `${trimZero((value / 1_000).toFixed(1))}K`
  return value.toLocaleString()
}

/** 单值紧凑(KPI 卡:组=自身) */
export function formatCompact(value: number): string {
  return formatGrouped(value, value)
}

/** Token 明细列:完整数字千分位,方便按行对账(§1.5 例外) */
export function formatTokensFull(value: number): string {
  return value.toLocaleString()
}

/** 0-1 比率 → 百分比文案(去尾零);digits 控制小数位。value 需 ×100 展示,delta_pp 不用(§1.2) */
export function formatRate(rate: number, digits = 2): string {
  return `${trimZero((rate * 100).toFixed(digits))}%`
}

/** 毫秒延迟:<700ms 显示 ms,≥700ms 显示 s(设计稿口径 710ms → 0.71s;去尾零) */
export function formatLatency(ms: number): string {
  return ms >= 700 ? `${trimZero((ms / 1000).toFixed(2))}s` : `${ms}ms`
}

/** delta 量纲(§1.2) */
export type DeltaKind = 'pct' | 'pp' | 'ms' | 'abs'

/** delta 量纲 → KPI 卡字段名 */
export const DELTA_FIELD: Record<DeltaKind, 'delta_pct' | 'delta_pp' | 'delta_ms' | 'delta'> = {
  pct: 'delta_pct',
  pp: 'delta_pp',
  ms: 'delta_ms',
  abs: 'delta',
}

/** 幅值 + 单位(不带符号——方向由卡片箭头表达) */
export function formatDelta(delta: number, kind: DeltaKind): string {
  if (kind === 'pct') return `${trimZero(Math.abs(delta).toFixed(1))}%`
  if (kind === 'pp') return `${trimZero(Math.abs(delta).toFixed(2))}pp`
  if (kind === 'ms') return `${Math.abs(Math.round(delta))}ms`
  return Math.abs(delta).toLocaleString()
}

/** 环比方向箭头(0 显示 ±) */
export function deltaArrow(delta: number): string {
  return delta > 0 ? '↑' : delta < 0 ? '↓' : '±'
}

/** 环比配色方向;badDirection=指标往哪边变是坏事 */
export type DeltaTone = 'good' | 'bad' | null
export function deltaTone(delta: number | null, badDirection: 'up' | 'down' | 'neutral'): DeltaTone {
  if (delta === null || delta === 0 || badDirection === 'neutral') return null
  const isUp = delta > 0
  if (badDirection === 'up') return isUp ? 'bad' : 'good'
  return isUp ? 'good' : 'bad'
}

/** 累计桶差分(§3.2):第 i 桶实际计数 = buckets[i] − buckets[i−1](首桶 = buckets[0]) */
export function diffCumulative(buckets: number[]): number[] {
  return buckets.map((bucket, index) => bucket - (index > 0 ? buckets[index - 1] : 0))
}

/** 屏1 小时双线(§2.2):hour 稀疏 → 0-23 连续铺,缺的小时补 0 */
export interface PaddedHourPoint {
  hour: number
  calls: number
  calls_yesterday: number
}
export function padHourTrend(points: OverviewTrendPointData[]): PaddedHourPoint[] {
  const byHour = new Map(points.map(point => [point.hour, point]))
  return Array.from({ length: 24 }, (_, hour) => {
    const point = byHour.get(hour)
    return { hour, calls: point?.calls ?? 0, calls_yesterday: point?.calls_yesterday ?? 0 }
  })
}

/** 构成图取色:颜色跟 key 走不跟排名走(§3.2/§7.6),key 稳定哈希,刷新/筛选后不重排色 */
export function colorForKey(key: string): string {
  if (key === OTHER_KEY) return OTHER_COLOR
  let hash = 0
  for (let i = 0; i < key.length; i += 1) hash = (hash * 31 + key.charCodeAt(i)) >>> 0
  return SERIES_PALETTE[hash % SERIES_PALETTE.length]!
}

/** 直方图 x 轴标签:首桶 <上界,中间桶按上界,末桶 >上一界(+Inf,§3.2) */
export function histLabels(
  bounds: Array<number | null>,
  format: (value: number) => string,
): string[] {
  return bounds.map((bound, index) => {
    if (index === bounds.length - 1) return `>${format(bounds[index - 1] ?? 0)}`
    return index === 0 ? `<${format(bound ?? 0)}` : format(bound ?? 0)
  })
}

/** token 桶上界标签:<1000 原样,≥1000 K(如 128 / 4K / 32K+) */
export function tokenBoundLabel(value: number): string {
  return value >= 1000 ? `${trimZero((value / 1000).toFixed(0))}K` : String(value)
}

/** 趋势槽起点 → x 轴标签(逐日粒度 M/D,小时粒度 M/D HH:00,其余 HH:mm;§1.4 granularity 驱动) */
export function formatSlotLabel(ts: string, granularity: string): string {
  const date = new Date(ts)
  if (Number.isNaN(date.getTime())) return ts
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  if (granularity === '1d') return `${date.getMonth() + 1}/${date.getDate()}`
  if (granularity === '1h') return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:00`
  return hm
}

// ============ KPI 卡构建(三屏共用) ============

/** KPI 卡原始数据(后端 OverviewKpiData/RealtimeKpiData 的公共字段超集,可选字段可缺省) */
export interface KpiCardRaw {
  value: number | null
  delta_pct?: number | null
  delta?: number | null
  delta_pp?: number | null
  delta_ms?: number | null
  week_total?: number | null
  peak?: number | null
  instance_id?: string | null
}

/** KPI 卡视图模型 */
export interface KpiCard {
  label: string
  value: string
  /** 环比文案(如「↑ 12.3% vs 昨日」);null = 无对照窗,隐藏环比区 */
  deltaText: string | null
  /** 环比配色 */
  deltaTone: DeltaTone
  /** 附加说明(如「本周累计 8 家」「最忙 #2」) */
  note: string | null
}

/** 单卡配置 */
export interface KpiCardSpec {
  key: string
  label: string
  kind: DeltaKind | null
  /** 指标往哪个方向变是坏事(决定环比配色) */
  badDirection: 'up' | 'down' | 'neutral'
  /** value 格式化;raw 透传原始卡(peak/instance_id 等附加字段要用) */
  format: (value: number, raw: KpiCardRaw) => string
  /** 附加说明回调 */
  note?: (raw: KpiCardRaw) => string | null
}

const EMPTY_RAW: KpiCardRaw = { value: null }

/** KPI 卡统一构建:null → —,delta 按量纲取字段,方向由箭头表达 */
export function buildKpiCard(spec: KpiCardSpec, raw: KpiCardRaw | undefined, deltaSuffix = ''): KpiCard {
  const source = raw ?? EMPTY_RAW
  const value = source.value ?? null
  const delta = spec.kind === null ? null : source[DELTA_FIELD[spec.kind]] ?? null
  return {
    label: spec.label,
    value: value === null ? EMPTY_TEXT : spec.format(value, source),
    // kind=null 时 delta 恒 null,故 delta 非 null 处 kind 必非 null
    deltaText:
      delta === null ? null : `${deltaArrow(delta)} ${formatDelta(delta, spec.kind!)}${deltaSuffix}`,
    deltaTone: deltaTone(delta, spec.badDirection),
    note: spec.note?.(source) ?? null,
  }
}

// ============ 窗口词表(屏2/屏3 的 /realtime 共用) ============

/** 窗口选择值:后端窗口词表 + 自定义区间哨兵(custom 需配 customRange 换算 from/to) */
export type WindowValue = RealtimeWindow | 'custom'

/** 自定义日期区间:[from, to] 毫秒时间戳(直配 n-date-picker 的 range value) */
export type CustomRange = [number, number]

/** 自定义区间跨度上限(/realtime 契约:跨度 > 90 天 → 422) */
export const CUSTOM_RANGE_MAX_MS = 90 * 24 * 3_600_000

/**
 * 日历格禁用判断:未来日期不可选,只按自然日比较(当日零点归一)。
 * 入参有两种形态——面板日历格传当天零点;value 合法性校验传带时刻的真实值
 * (naive dualCalendarValidation),归一后当日任意时刻均合法,否则快捷窗口值会被判非法置灰。
 */
export function isFutureDate(ts: number): boolean {
  const day = new Date(ts)
  day.setHours(0, 0, 0, 0)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return day.getTime() > today.getTime()
}

/** 窗口 tab 选项(§6:6 个快捷窗口;custom 不占 tab,由页头日期选择器确认后进入) */
export const WINDOW_OPTIONS: Array<{ value: WindowValue; label: string }> = [
  { value: 'today', label: '今日' },
  { value: '1h', label: '近1小时' },
  { value: '24h', label: '近24小时' },
  { value: '7d', label: '近7天' },
  { value: '14d', label: '近14天' },
  { value: '30d', label: '近30天' },
]

/** 环比文案(§5):today=较昨日同期,滚动窗=较上一时段,多日窗/自定义 null */
export function compareLabel(window: WindowValue): string | null {
  if (window === 'today') return '较昨日同期'
  if (window === '1h' || window === '24h') return '较上一时段'
  return null
}

/** 窗口词表 → 区间毫秒跨度(today 按本地零点起,其余滚动窗;§2 缺省语义的前端等价实现) */
export function windowToMs(window: RealtimeWindow, now = new Date()): number {
  if (window === 'today') return now.getTime() - new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  if (window === '1h') return 3_600_000
  if (window === '24h') return 24 * 3_600_000
  return Number(window.replace('d', '')) * 24 * 3_600_000
}

/**
 * 窗口选择值 → /realtime 的 from/to + prev_from/prev_to(2026-09-17 区间化)。
 * 对照窗 = 当前区间整体平移一个「时段」:1h→前 1 小时,today/24h→前 24 小时
 * (today 平移 24h 即「昨日同期」[昨日00:00, 昨日此刻];多日窗/自定义不传 prev_*,后端 delta_* 全 null)。
 * 多日窗(7d/14d/30d)起点对齐到 N 天前的本地零点(与 today 同为自然日语义)。
 */
export function windowToRealtimeQuery(
  window: WindowValue,
  now = new Date(),
  customRange?: CustomRange | null,
): { from: string; to: string; prev_from?: string; prev_to?: string } {
  // 自定义区间:from/to 原样换算;区间缺失/倒置时按「今日零点 → now」兜底(正常流程 custom 必带区间)
  if (window === 'custom') {
    const range =
      customRange && customRange[1] > customRange[0]
        ? customRange
        : ([new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime(), now.getTime()] as CustomRange)
    return { from: new Date(range[0]).toISOString(), to: new Date(range[1]).toISOString() }
  }
  const to = new Date(now)
  let from: Date
  if (window === 'today') {
    from = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  } else if (window === '1h' || window === '24h') {
    from = new Date(to.getTime() - windowToMs(window, now))
  } else {
    from = new Date(now)
    from.setDate(from.getDate() - Number(window.replace('d', '')))
    from.setHours(0, 0, 0, 0)
  }
  const result: { from: string; to: string; prev_from?: string; prev_to?: string } = {
    from: from.toISOString(),
    to: to.toISOString(),
  }
  if (compareLabel(window) !== null) {
    const shiftMs = window === '1h' ? 3_600_000 : 24 * 3_600_000
    result.prev_from = new Date(from.getTime() - shiftMs).toISOString()
    result.prev_to = new Date(to.getTime() - shiftMs).toISOString()
  }
  return result
}

/** 窗口选择值 → /models、/tokens 的 from/to(custom 直传区间;多日窗起点对齐零点) */
export function windowToRange(
  window: WindowValue,
  now = new Date(),
  customRange?: CustomRange | null,
): { from: string; to: string } {
  if (window === 'custom') {
    const range =
      customRange && customRange[1] > customRange[0]
        ? customRange
        : ([new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime(), now.getTime()] as CustomRange)
    return { from: new Date(range[0]).toISOString(), to: new Date(range[1]).toISOString() }
  }
  const to = new Date(now)
  const from = new Date(now)
  if (window === 'today') from.setHours(0, 0, 0, 0)
  else if (window === '1h') from.setHours(from.getHours() - 1)
  else if (window === '24h') from.setHours(from.getHours() - 24)
  else {
    from.setDate(from.getDate() - Number(window.replace('d', '')))
    from.setHours(0, 0, 0, 0)
  }
  return { from: from.toISOString(), to: to.toISOString() }
}
