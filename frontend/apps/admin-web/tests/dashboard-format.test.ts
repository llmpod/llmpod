import { describe, expect, it } from 'vitest'
import {
  buildKpiCard,
  colorForKey,
  compareLabel,
  deltaArrow,
  deltaTone,
  DELTA_FIELD,
  diffCumulative,
  EMPTY_TEXT,
  formatCompact,
  formatDelta,
  formatGrouped,
  formatLatency,
  formatRate,
  formatSlotLabel,
  formatTokensFull,
  histLabels,
  maxOf,
  OTHER_COLOR,
  OTHER_KEY,
  padHourTrend,
  tokenBoundLabel,
  windowToRange,
  windowToRealtimeQuery,
} from '@/views/dashboard-shared/format'

describe('formatGrouped(§1.5 按组内最大值定单位)', () => {
  it('组最大值 <10万 → 次数千分位', () => {
    expect(formatGrouped(60_000, 60_000)).toBe('60,000')
    expect(formatGrouped(0, 99_999)).toBe('0')
  })
  it('组最大值 10万~1000万 → K(1 位小数去尾零);≥1000万 → M', () => {
    expect(formatGrouped(612_400, 612_400)).toBe('612.4K')
    expect(formatGrouped(84_000, 612_400)).toBe('84K')
    expect(formatGrouped(48_200_000, 48_200_000)).toBe('48.2M')
  })
})

describe('formatCompact / formatLatency / formatRate', () => {
  it('单值紧凑:组=自身', () => {
    expect(formatCompact(128_400)).toBe('128.4K')
    expect(formatCompact(64)).toBe('64')
  })
  it('延迟:<700 显示 ms,≥700 显示 s(去尾零)', () => {
    expect(formatLatency(185)).toBe('185ms')
    expect(formatLatency(1850)).toBe('1.85s')
    expect(formatLatency(610)).toBe('610ms')
    expect(formatLatency(700)).toBe('0.7s')
    expect(formatLatency(710)).toBe('0.71s')
  })
  it('比率 0-1 → 百分比;>1 也照实(异常占比 216.7%)', () => {
    expect(formatRate(0.9924)).toBe('99.24%')
    expect(formatRate(1)).toBe('100%')
    expect(formatRate(2.1667, 1)).toBe('216.7%')
    expect(formatRate(0.9, 0)).toBe('90%')
    expect(formatRate(1, 0)).toBe('100%')
  })
})

describe('delta 四量纲(§1.2:幅度不带符号,方向由箭头表达)', () => {
  it('pct/pp/ms/abs', () => {
    expect(formatDelta(12.34, 'pct')).toBe('12.3%')
    expect(formatDelta(-0.4, 'pp')).toBe('0.4pp')
    expect(formatDelta(-20.4, 'ms')).toBe('20ms')
    expect(formatDelta(107, 'abs')).toBe('107')
  })
  it('箭头与配色方向', () => {
    expect(deltaArrow(5)).toBe('↑')
    expect(deltaArrow(-1)).toBe('↓')
    expect(deltaArrow(0)).toBe('±')
    expect(deltaTone(5, 'up')).toBe('bad')
    expect(deltaTone(-5, 'up')).toBe('good')
    expect(deltaTone(5, 'down')).toBe('good')
    expect(deltaTone(null, 'up')).toBeNull()
    expect(deltaTone(3, 'neutral')).toBeNull()
  })
})

describe('diffCumulative(§3.2 累计桶差分)', () => {
  it('第 i 桶 = buckets[i] − buckets[i−1]', () => {
    expect(diffCumulative([10, 30, 30, 55])).toEqual([10, 20, 0, 25])
  })
})

describe('padHourTrend(§2.2 稀疏小时 0-23 连续铺)', () => {
  it('缺小时补 0,长度恒 24', () => {
    const padded = padHourTrend([
      { hour: 9, calls: 3, calls_yesterday: 1 },
      { hour: 12, calls: 34, calls_yesterday: 0 },
    ])
    expect(padded).toHaveLength(24)
    expect(padded[9]).toEqual({ hour: 9, calls: 3, calls_yesterday: 1 })
    expect(padded[10]).toEqual({ hour: 10, calls: 0, calls_yesterday: 0 })
    expect(padded[12]!.calls_yesterday).toBe(0)
  })
})

describe('colorForKey(颜色跟 key 走不跟排名走)', () => {
  it('同 key 稳定、__other__ 固定灰', () => {
    expect(colorForKey('qwen2.5-7b')).toBe(colorForKey('qwen2.5-7b'))
    expect(colorForKey(OTHER_KEY)).toBe(OTHER_COLOR)
    expect(colorForKey('a')).not.toBe(colorForKey('b'))
  })
})

describe('直方图轴标签', () => {
  it('首桶 <上界、末桶 >上一界(+Inf)', () => {
    expect(histLabels([200, 1000, 30_000, null], formatLatency)).toEqual([
      '<200ms',
      '1s',
      '30s',
      '>30s',
    ])
  })
  it('token 上界:<1000 原样,≥1000 K', () => {
    expect(tokenBoundLabel(128)).toBe('128')
    expect(tokenBoundLabel(4096)).toBe('4K')
    expect(tokenBoundLabel(10_000)).toBe('10K')
  })
})

describe('formatSlotLabel(granularity 驱动横轴标签)', () => {
  it('5m → HH:mm;1h → M/D HH:00;1d → M/D', () => {
    const pad = (n: number) => String(n).padStart(2, '0')
    const ts = '2026-09-15T13:05:00+08:00'
    const d = new Date(ts)
    expect(formatSlotLabel(ts, '5m')).toBe(`${pad(d.getHours())}:${pad(d.getMinutes())}`)
    expect(formatSlotLabel(ts, '1h')).toBe(`${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:00`)
    expect(formatSlotLabel(ts, '1d')).toBe(`${d.getMonth() + 1}/${d.getDate()}`)
  })
})

describe('buildKpiCard', () => {
  const spec = {
    key: 'calls',
    label: '调用量 · 今日',
    kind: 'pct' as const,
    badDirection: 'down' as const,
    format: (value: number) => String(value),
  }
  it('正常卡:value 格式化、delta 带箭头与后缀、配色按方向', () => {
    const card = buildKpiCard(
      spec,
      { value: 64, delta_pct: 12.3, delta: null, delta_pp: null, delta_ms: null },
      ' vs 昨日',
    )
    expect(card).toEqual({
      label: '调用量 · 今日',
      value: '64',
      deltaText: '↑ 12.3% vs 昨日',
      deltaTone: 'good',
      note: null,
    })
  })
  it('无卡/无样本 → — 且隐藏环比;kind=null 恒无环比', () => {
    expect(buildKpiCard(spec, undefined).value).toBe(EMPTY_TEXT)
    expect(buildKpiCard(spec, undefined).deltaText).toBeNull()
    expect(
      buildKpiCard(
        { ...spec, kind: null },
        { value: 3, delta_pct: null, delta: null, delta_pp: null, delta_ms: null },
      ).deltaText,
    ).toBeNull()
  })
  it('note 回调(new_tenants 本周累计)', () => {
    const card = buildKpiCard(
      { ...spec, kind: null, note: raw => (raw.week_total == null ? null : `本周累计 ${raw.week_total} 家`) },
      { value: 3, delta_pct: null, delta: null, delta_pp: null, delta_ms: null, week_total: 8 },
    )
    expect(card.note).toBe('本周累计 8 家')
  })
})

describe('maxOf', () => {
  it('空数组 0', () => {
    expect(maxOf([])).toBe(0)
    expect(maxOf([3, 9, 1])).toBe(9)
  })
})

describe('窗口词表与 token 完整数字', () => {
  it('formatTokensFull 千分位', () => {
    expect(formatTokensFull(12_142_880)).toBe('12,142,880')
  })
  it('compareLabel:today 较昨日同期,滚动窗较上一时段,多日/自定义 null', () => {
    expect(compareLabel('today')).toBe('较昨日同期')
    expect(compareLabel('1h')).toBe('较上一时段')
    expect(compareLabel('24h')).toBe('较上一时段')
    expect(compareLabel('7d')).toBeNull()
    expect(compareLabel('30d')).toBeNull()
    expect(compareLabel('custom')).toBeNull()
  })
  it('windowToRange:词表 → from/to(注入 now 断言时长与时点)', () => {
    const now = new Date('2026-09-15T14:30:00')
    const oneHour = windowToRange('1h', now)
    expect(new Date(oneHour.to).getTime() - new Date(oneHour.from).getTime()).toBe(3_600_000)
    // 多日窗起点对齐零点:7d = 9/8 00:00 → now
    const sevenDays = windowToRange('7d', now)
    const sevenFrom = new Date(now)
    sevenFrom.setDate(sevenFrom.getDate() - 7)
    sevenFrom.setHours(0, 0, 0, 0)
    expect(Date.parse(sevenDays.from)).toBe(sevenFrom.getTime())
    expect(Date.parse(sevenDays.to)).toBe(now.getTime())
    const today = windowToRange('today', now)
    expect(new Date(today.from).getHours()).toBe(0)
  })
  it('windowToRealtimeQuery:today 平移 24h 得昨日同期;1h/24h 平移一个时段;多日窗不带 prev', () => {
    const now = new Date('2026-09-15T14:30:00')
    const MS = { hour: 3_600_000, day: 24 * 3_600_000 }

    // today:from = 本地今日 00:00,prev = [昨日00:00, 昨日此刻](平移 24h,等长)
    const today = windowToRealtimeQuery('today', now)
    const todayFrom = new Date(today.from)
    expect(`${todayFrom.getHours()}:${todayFrom.getMinutes()}`).toBe('0:0')
    expect(Date.parse(today.to)).toBe(now.getTime())
    const todaySpan = now.getTime() - todayFrom.getTime()
    expect(Date.parse(today.prev_from!)).toBe(todayFrom.getTime() - MS.day)
    expect(Date.parse(today.prev_to!)).toBe(now.getTime() - MS.day)
    expect(Date.parse(today.prev_to!) - Date.parse(today.prev_from!)).toBe(todaySpan)

    // 1h:[now-1h, now],prev = [now-2h, now-1h](紧邻上一时段)
    const oneHour = windowToRealtimeQuery('1h', now)
    const from1h = Date.parse(oneHour.from)
    expect(Date.parse(oneHour.to) - from1h).toBe(MS.hour)
    expect(Date.parse(oneHour.prev_from!)).toBe(from1h - MS.hour)
    expect(Date.parse(oneHour.prev_to!)).toBe(now.getTime() - MS.hour)

    // 24h:prev = [now-48h, now-24h]
    const day = windowToRealtimeQuery('24h', now)
    const from24h = Date.parse(day.from)
    expect(Date.parse(day.to) - from24h).toBe(MS.day)
    expect(Date.parse(day.prev_from!)).toBe(from24h - MS.day)
    expect(Date.parse(day.prev_to!)).toBe(now.getTime() - MS.day)

    // 多日窗:不传 prev_*(后端 delta_* 全 null);起点对齐 N 天前本地零点(跨度 = N 天 + 当天已过时刻)
    for (const window of ['7d', '14d', '30d'] as const) {
      const multiDay = windowToRealtimeQuery(window, now)
      expect(multiDay.prev_from).toBeUndefined()
      expect(multiDay.prev_to).toBeUndefined()
      const expectFrom = new Date(now)
      expectFrom.setDate(expectFrom.getDate() - Number(window.replace('d', '')))
      expectFrom.setHours(0, 0, 0, 0)
      expect(Date.parse(multiDay.from)).toBe(expectFrom.getTime())
      expect(Date.parse(multiDay.to)).toBe(now.getTime())
    }
  })
  it('custom 窗口:from/to 原样换算、不带 prev_*;区间缺失时按今日零点兜底', () => {
    const now = new Date('2026-09-15T14:30:00')
    const from = new Date('2026-08-01T08:00:00').getTime()
    const to = new Date('2026-09-10T20:00:00').getTime()

    const query = windowToRealtimeQuery('custom', now, [from, to])
    expect(Date.parse(query.from)).toBe(from)
    expect(Date.parse(query.to)).toBe(to)
    expect(query.prev_from).toBeUndefined()
    expect(query.prev_to).toBeUndefined()

    const range = windowToRange('custom', now, [from, to])
    expect(Date.parse(range.from)).toBe(from)
    expect(Date.parse(range.to)).toBe(to)

    // 区间缺失/倒置 → 今日零点 → now 兜底(与 today 语义一致,请求不至于发不出去)
    const fallback = windowToRealtimeQuery('custom', now, null)
    expect(fallback.prev_from).toBeUndefined()
    const fallbackFrom = new Date(fallback.from)
    expect(`${fallbackFrom.getHours()}:${fallbackFrom.getMinutes()}`).toBe('0:0')
    expect(Date.parse(fallback.to)).toBe(now.getTime())
  })
  it('DELTA_FIELD 量纲映射', () => {
    expect(DELTA_FIELD.pct).toBe('delta_pct')
    expect(DELTA_FIELD.pp).toBe('delta_pp')
    expect(DELTA_FIELD.ms).toBe('delta_ms')
    expect(DELTA_FIELD.abs).toBe('delta')
  })
})
