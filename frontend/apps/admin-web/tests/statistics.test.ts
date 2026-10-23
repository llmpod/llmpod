// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import {
  getDashboardModels,
  getDashboardRealtime,
  getDashboardTokens,
  type RealtimeData,
  type RealtimeKpiData,
  type TokensData,
} from '@/api/dashboard'
import { listApiKeys } from '@/api/api-key'
import { listPublishes } from '@/api/publish'
import { listTenants } from '@/api/tenant'
import { OTHER_KEY, windowToRange } from '@/views/dashboard-shared/format'
import { useStore } from '@/views/statistics/useStore'
import { useController } from '@/views/statistics/useController'

vi.mock('@/api/dashboard', () => ({
  getDashboardRealtime: vi.fn(),
  getDashboardModels: vi.fn(),
  getDashboardTokens: vi.fn(),
}))

vi.mock('@/api/publish', () => ({
  listPublishes: vi.fn().mockResolvedValue([
    { id: 1, route: { name: 'qwen2.5-7b' } },
    { id: 2, route: { name: 'llama-3.1-8b' } },
  ]),
}))

vi.mock('@/api/tenant', () => ({
  listTenants: vi.fn().mockResolvedValue({
    items: [
      { id: 3, name: '企业A' },
      { id: 4, name: '企业B' },
    ],
    total: 2,
    page: 1,
    page_size: 100,
    total_pages: 1,
  }),
}))

vi.mock('@/api/api-key', () => ({
  listApiKeys: vi.fn().mockResolvedValue([
    { id: 7, name: 'prod-key' },
    { id: 8, name: 'dev-key' },
  ]),
}))

// happy-dom 无 canvas:图表桩掉 useChart(el 保留 ref 供模板绑定,render/dispose 为桩函数)
vi.mock('@/views/dashboard-shared/charts', async (importOriginal) => {
  const { ref } = await import('vue')
  const actual = await importOriginal<typeof import('@/views/dashboard-shared/charts')>()
  return {
    ...actual,
    useChart: () => ({ el: ref(), render: vi.fn(), dispose: vi.fn() }),
  }
})

const store = useStore()

/** 空 delta 模板:保证 RealtimeKpiData 七字段齐全(value + 四个 delta + peak/instance_id) */
function kpi(value: number | null, extra: Partial<RealtimeKpiData> = {}): RealtimeKpiData {
  return {
    value,
    delta_pct: null,
    delta: null,
    delta_pp: null,
    delta_ms: null,
    peak: null,
    instance_id: null,
    ...extra,
  }
}

function makeRealtime(overrides: Partial<RealtimeData> = {}): RealtimeData {
  return {
    ts: '2026-09-15T14:30:00+08:00',
    window: {
      from: '2026-09-15T08:30:00+08:00',
      to: '2026-09-15T14:30:00+08:00',
      granularity: '5m',
      source: 'cold',
      age_ms: null,
    },
    kpi: {
      qps: kpi(10),
      tpm: kpi(200),
      calls: kpi(612_400, { delta_pct: 8.2 }),
      total_tokens_kpi: kpi(48_200_000, { delta_pct: 6.1 }),
      avg_context: kpi(1_800, { delta: 120 }),
      error_rate: kpi(0.0076, { delta_pp: -0.4 }),
      truncation_rate: kpi(0.014, { delta_pp: 0.3 }),
      latency_avg_ms: kpi(710, { delta_ms: 12 }),
      ttft_avg_ms: kpi(380),
      p95_ms: kpi(null),
      cache_hit_rate: kpi(null),
      output_tps: kpi(null),
      in_flight: kpi(null),
      kv_cache_util: kpi(null),
      waiting_requests: kpi(null),
    },
    trend: {
      baseline: {},
      points: [
        {
          ts: '2026-09-15T14:00:00+08:00',
          qpm: 10,
          tpm: 200,
          prompt_tpm: 150,
          completion_tpm: 50,
          requests: 100,
          latency_avg_ms: 700,
        },
        {
          ts: '2026-09-15T14:05:00+08:00',
          qpm: 12,
          tpm: 240,
          prompt_tpm: 180,
          completion_tpm: 60,
          requests: 120,
          latency_avg_ms: null,
        },
      ],
    },
    composition: {
      dim: 'model',
      total: [
        { ts: '2026-09-15T14:00:00+08:00', tpm: 200 },
        { ts: '2026-09-15T14:05:00+08:00', tpm: 240 },
      ],
      items: [
        {
          key: 'qwen2.5-7b',
          tpm: [
            { ts: '2026-09-15T14:00:00+08:00', value: 150 },
            { ts: '2026-09-15T14:05:00+08:00', value: 180 },
          ],
          requests: [
            { ts: '2026-09-15T14:00:00+08:00', value: 70 },
            { ts: '2026-09-15T14:05:00+08:00', value: 80 },
          ],
        },
        {
          key: OTHER_KEY,
          tpm: [
            { ts: '2026-09-15T14:00:00+08:00', value: 50 },
            { ts: '2026-09-15T14:05:00+08:00', value: 60 },
          ],
          requests: [
            { ts: '2026-09-15T14:00:00+08:00', value: 30 },
            { ts: '2026-09-15T14:05:00+08:00', value: 40 },
          ],
        },
      ],
    },
    distribution: {
      latency: { buckets: [10, 30, 30, 55], bounds: [200, 1000, 30_000, null], avg_ms: 710, slow_ratio: 0.02 },
      ttft: { buckets: [20, 45, 45, 60], bounds: [200, 1000, 10_000, null], avg_ms: 380, slow_ratio: 0.05 },
      prompt_len: null,
      generation_len: { buckets: [5, 15, 20], bounds: [128, 4096, null], avg_tokens: 260 },
    },
    engines: [],
    ...overrides,
  }
}

function makeTokens(overrides: Partial<TokensData> = {}): TokensData {
  return {
    dim: 'model',
    from: '2026-09-15T00:00:00+08:00',
    to: '2026-09-15T14:30:00+08:00',
    rows: [
      {
        key: 'qwen2.5-7b',
        input_tokens: 12_142_880,
        output_tokens: 4_842_110,
        cached_tokens: 5_214_880,
        total_tokens: 16_984_990,
        ratio: 0.51,
      },
      {
        key: 'llama-3.1-8b',
        input_tokens: 8_981_204,
        output_tokens: 2_824_556,
        cached_tokens: 2_981_204,
        total_tokens: 11_805_760,
        ratio: 0.23,
      },
    ],
    other: {
      key: '其他 5 个模型',
      input_tokens: 10_468_652,
      output_tokens: 1_919_140,
      cached_tokens: 3_536_652,
      total_tokens: 12_387_792,
      ratio: 0.26,
      count: 5,
    },
    total: {
      input_tokens: 31_592_736,
      output_tokens: 9_585_806,
      cached_tokens: 11_732_736,
      total_tokens: 41_178_542,
    },
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  store.activeWindow.value = 'today'
  store.customRange.value = null
  store.filterModel.value = ''
  store.filterTenantId.value = null
  store.filterApiKey.value = null
  vi.mocked(getDashboardRealtime).mockResolvedValue(makeRealtime())
  vi.mocked(getDashboardModels).mockResolvedValue([
    {
      model: 'qwen2.5-7b',
      call_count: 312_400,
      ratio: 0.51,
      avg_latency_ms: 680,
      avg_ttft_ms: 380,
      error_rate: 0.0032,
      truncation_rate: 0.012,
      tokens: 16_984_990,
    },
    {
      model: 'llama-3.1-8b',
      call_count: 142_100,
      ratio: 0.23,
      avg_latency_ms: 920,
      avg_ttft_ms: null,
      error_rate: 0.0181,
      truncation_rate: null,
      tokens: 11_805_760,
    },
  ])
  vi.mocked(getDashboardTokens).mockResolvedValue(makeTokens())
})

afterEach(() => {
  vi.useRealTimers()
})

describe('statistics useStore(/admin/dashboard/realtime 对接)', () => {
  /** 取最近一次调用的 params */
  function realtimeCall(): NonNullable<Parameters<typeof getDashboardRealtime>[0]> {
    return vi.mocked(getDashboardRealtime).mock.calls.at(-1)?.[0]!
  }

  it('fetchAll:参数下推(默认 today + model 维度,区间 + 昨日同期对照窗),KPI 六卡含派生成功率', async () => {
    await store.fetchAll()

    const params = realtimeCall()
    expect(params.dim).toBe('model')
    // 2026-09-17 区间化:window/granularity 已下线(传了 422)
    expect(params.window).toBeUndefined()
    expect(params.granularity).toBeUndefined()
    // today:from = 本地今日 00:00,to = 当前;prev_* = 整体平移 24h 的昨日同期(等长)
    const from = new Date(params.from!)
    expect(`${from.getHours()}:${from.getMinutes()}:${from.getSeconds()}`).toBe('0:0:0')
    const fromMs = from.getTime()
    const toMs = Date.parse(params.to!)
    const spanMs = toMs - fromMs
    expect(spanMs).toBeGreaterThan(0)
    expect(spanMs).toBeLessThanOrEqual(24 * 3_600_000)
    expect(Date.parse(params.prev_from!)).toBe(fromMs - 24 * 3_600_000)
    expect(Date.parse(params.prev_to!)).toBe(toMs - 24 * 3_600_000)

    const cards = store.statCards.value
    expect(cards.map(card => card.label)).toEqual([
      '调用量',
      '成功率',
      '平均延迟',
      '总 Token',
      '平均上下文',
      '截断率',
    ])
    // 成功率 = 1 − error_rate;delta_pp 取反后方向取反(error_rate 降 → success 升 → good)
    expect(cards[0]).toMatchObject({ value: '612.4K', deltaText: '↑ 8.2% 较昨日同期', deltaTone: 'good' })
    expect(cards[1]).toMatchObject({ value: '99.24%', deltaText: '↑ 0.4pp 较昨日同期', deltaTone: 'good' })
    expect(cards[2]).toMatchObject({ value: '0.71s', deltaText: '↑ 12ms 较昨日同期', deltaTone: 'bad' })
    expect(cards[3]).toMatchObject({ value: '48.2M' })
    expect(cards[4]).toMatchObject({ value: '1,800', deltaText: '↑ 120 较昨日同期', deltaTone: null })
    expect(cards[5]).toMatchObject({ value: '1.4%', deltaText: '↑ 0.3pp 较昨日同期', deltaTone: 'bad' })
  })

  it('多日窗不带 prev(后端 delta 全 null,前端抹掉环比);起点对齐零点;滚动窗文案为「较上一时段」(§5)', async () => {
    store.activeWindow.value = '7d'
    await store.fetchAll()
    const params = realtimeCall()
    expect(params.prev_from).toBeUndefined()
    expect(params.prev_to).toBeUndefined()
    // 7d 起点 = 7 天前本地零点(不再是恰好 7×24h 滚动窗)
    const from = new Date()
    from.setDate(from.getDate() - 7)
    from.setHours(0, 0, 0, 0)
    expect(Date.parse(params.from!)).toBe(from.getTime())
    expect(store.statCards.value.every(card => card.deltaText === null)).toBe(true)

    store.activeWindow.value = '1h'
    await store.fetchAll()
    const rolling = realtimeCall()
    expect(Date.parse(rolling.to!) - Date.parse(rolling.from!)).toBe(3_600_000)
    expect(store.statCards.value[0]!.deltaText).toContain('较上一时段')
  })

  it('自定义窗口:区间原样下推 /realtime 与 /models,无 prev、环比隐藏', async () => {
    const from = new Date('2026-09-01T00:00:00.000').getTime()
    const to = new Date('2026-09-10T23:59:59.999').getTime()
    store.activeWindow.value = 'custom'
    store.customRange.value = [from, to]
    await store.fetchAll()
    const params = realtimeCall()
    expect(Date.parse(params.from!)).toBe(from)
    expect(Date.parse(params.to!)).toBe(to)
    expect(params.prev_from).toBeUndefined()
    expect(params.prev_to).toBeUndefined()
    expect(getDashboardModels).toHaveBeenCalledWith(expect.objectContaining({ from: params.from, to: params.to }), undefined)
    expect(store.statCards.value.every(card => card.deltaText === null)).toBe(true)
  })

  it('筛选整屏下推(model/tenant/api_key);空串与 null 不带参', async () => {
    store.filterModel.value = 'qwen2.5-7b'
    store.filterTenantId.value = 3
    store.filterApiKey.value = 7
    await store.fetchAll()
    expect(getDashboardRealtime).toHaveBeenCalledWith(
      expect.objectContaining({ dim: 'model', model: 'qwen2.5-7b', tenant_id: 3, api_key_id: 7 }),
      undefined,
    )
    expect(getDashboardModels).toHaveBeenCalledWith(
      expect.objectContaining({ model: 'qwen2.5-7b', tenant_id: 3 }),
      undefined,
    )
    expect(getDashboardTokens).toHaveBeenCalledWith(
      expect.objectContaining({ model: 'qwen2.5-7b', tenant_id: 3 }),
      undefined,
    )
  })

  it('堆叠视图:总量与构成同槽网格、y 轴计数轴、颜色跟 key(§3.2/§7.4)', async () => {
    await store.fetchAll()
    const stacked = store.stacked.value!
    expect(stacked.labels).toEqual(['14:00', '14:05'])
    expect(stacked.requests).toEqual([100, 120])
    expect(stacked.series.map(series => series.key)).toEqual(['qwen2.5-7b', OTHER_KEY])
    expect(stacked.series[0]!.values).toEqual([70, 80])
    expect(stacked.axisMax).toBe(120)
  })

  it('空窗趋势:points/items 任一为空 → stacked 为 null(空图表骨架,避免整块空白)', async () => {
    // 完全空窗:无时间槽、无构成
    vi.mocked(getDashboardRealtime).mockResolvedValue(
      makeRealtime({
        trend: { baseline: {}, points: [] },
        composition: { dim: 'model', total: [], items: [] },
      }),
    )
    await store.fetchAll()
    expect(store.stacked.value).toBeNull()

    // 有时间槽但无构成(series 为空数组时 value 轴不生成刻度区间,同样走骨架)
    vi.mocked(getDashboardRealtime).mockResolvedValue(
      makeRealtime({ composition: { dim: 'model', total: [], items: [] } }),
    )
    await store.fetchAll()
    expect(store.stacked.value).toBeNull()
  })

  it('四张分布:累计桶差分、轴标签、均值/慢占比;无样本空态(§3.2)', async () => {
    await store.fetchAll()
    const distributions = store.distributions.value
    expect(distributions.latency!.bars.map(bar => bar.count)).toEqual([10, 20, 0, 25])
    expect(distributions.latency!.labels).toEqual(['<200ms', '1s', '30s', '>30s'])
    expect(distributions.latency!.avgText).toBe('0.71s')
    expect(distributions.latency!.subText).toBe('慢请求 2%')
    expect(distributions.promptLen).toBeNull()
    expect(distributions.generationLen!.bars.map(bar => bar.count)).toEqual([5, 10, 5])
    expect(distributions.generationLen!.labels).toEqual(['<128', '4K', '>4K'])
    expect(distributions.generationLen!.avgText).toBe('260 tok')
  })

  it('模型统计宽表:/models + /tokens 按 key 合并、长尾折叠行、合计行取窗口级口径', async () => {
    await store.fetchAll()
    const rows = store.modelRows.value
    // 行序:/models top 行 + other 折叠行 + 合计行
    expect(rows).toHaveLength(4)
    expect(rows[0]).toMatchObject({
      model: 'qwen2.5-7b',
      calls: '312.4K',
      share: '51%',
      latency: '680ms',
      error: '0.32%',
      truncation: '1.2%',
      input: '12,142,880',
      output: '4,842,110',
      cached: '5,214,880',
      total: '17M',
      isTotal: false,
    })
    // 无样本列 → —(§1.1);920ms < 700 阈值?否——920 ≥ 700 → '0.92s'
    expect(rows[1]).toMatchObject({ model: 'llama-3.1-8b', truncation: '—', latency: '0.92s' })
    // 长尾折叠行(仅 token 列,统计列 —)
    expect(rows[2]).toMatchObject({
      model: '其他 5 个模型',
      calls: '—',
      share: '26%',
      total: '12.4M',
      isTotal: false,
    })
    // 合计行:调用量/错误率/截断率取窗口级 kpi,token 列取 tokens.total
    expect(rows[3]).toMatchObject({
      model: '合计',
      calls: '612.4K',
      share: '100%',
      error: '0.76%',
      truncation: '1.4%',
      input: '31,592,736',
      output: '9,585,806',
      cached: '11,732,736',
      total: '41.2M',
      isTotal: true,
    })
  })

  it('数据源徽标:hot 显示秒龄,cold 显示 5 分钟桶(§3.0)', async () => {
    await store.fetchAll()
    expect(store.sourceText.value).toBe('5 分钟桶')

    vi.mocked(getDashboardRealtime).mockResolvedValue(
      makeRealtime({
        window: { from: '', to: '', granularity: '30s', source: 'hot', age_ms: 4200 },
      }),
    )
    await store.fetchAll()
    expect(store.sourceText.value).toBe('实时 · 4s 前')
  })

  it('windowToRange:词表 → from/to(时长断言,不依赖时区)', () => {
    const now = new Date('2026-09-15T14:30:00')
    const oneHour = windowToRange('1h', now)
    expect(new Date(oneHour.to).getTime() - new Date(oneHour.from).getTime()).toBe(3_600_000)
    const sevenDays = windowToRange('7d', now)
    const sevenFrom = new Date(now)
    sevenFrom.setDate(sevenFrom.getDate() - 7)
    sevenFrom.setHours(0, 0, 0, 0)
    expect(Date.parse(sevenDays.from)).toBe(sevenFrom.getTime())
    const today = windowToRange('today', now)
    expect(new Date(today.from).getHours()).toBe(0)
  })
})

describe('statistics useController', () => {
  function mountController() {
    let controller!: ReturnType<typeof useController>
    const wrapper = mount({
      setup: () => {
        controller = useController()
        return () => h('div')
      },
    })
    return { controller, wrapper }
  }

  it('首轮拉取完成 loading 置 false;筛选选项并发加载', async () => {
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(listPublishes).toHaveBeenCalled()
    expect(listTenants).toHaveBeenCalledWith({ page_size: 100 })
    expect(listApiKeys).toHaveBeenCalled()
    // 首项固定「全部」(value '' 与 clearable 清空对齐;基线既有断言过期,此处对齐现状)
    expect(controller.modelOptions.value.map(option => option.label)).toEqual(['全部', 'qwen2.5-7b', 'llama-3.1-8b'])
    expect(controller.tenantOptions.value).toHaveLength(2)
    expect(controller.apiKeyOptions.value).toHaveLength(2)
    wrapper.unmount()
  })

  it('10s 静默轮询 /realtime(§3.0:≤10s 引擎稳快档)', async () => {
    vi.useFakeTimers()
    const { wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    expect(getDashboardRealtime).toHaveBeenCalledTimes(1)

    vi.mocked(getDashboardRealtime).mockClear()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getDashboardRealtime).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getDashboardRealtime).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('窗口切换:回骨架屏、带新窗口重拉、环比文案切换(§5/§6)', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    expect(controller.compareText.value).toBe('较昨日同期')

    controller.setWindow('1h')
    expect(controller.loading.value).toBe(true)
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    const oneHourParams = vi.mocked(getDashboardRealtime).mock.calls.at(-1)?.[0]!
    expect(oneHourParams.dim).toBe('model')
    expect(Date.parse(oneHourParams.to!) - Date.parse(oneHourParams.from!)).toBe(3_600_000)
    expect(controller.compareText.value).toBe('较上一时段')

    controller.setWindow('7d')
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(controller.compareText.value).toBeNull()
    wrapper.unmount()
  })

  it('自定义窗口:date-picker 常驻展示快捷窗口实际区间;提交区间切 custom 重拉;超 90 天被拒;清空回退今日', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))

    // 快捷窗口(today)下展示区间 = 该窗口实际区间(今日零点 → now)
    const shown = controller.customRange.value!
    expect(shown[1] - shown[0]).toBeGreaterThan(0)
    expect(shown[1] - shown[0]).toBeLessThanOrEqual(24 * 3_600_000)

    // 提交合法区间(含时间部分):原样下推切 custom 重拉,无 prev、环比隐藏
    const from = Date.now() - 48 * 3_600_000
    const to = Date.now()
    controller.setCustomRange([from, to])
    expect(controller.activeWindow.value).toBe('custom')
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    const params = vi.mocked(getDashboardRealtime).mock.calls.at(-1)?.[0]!
    expect(Date.parse(params.from!)).toBe(from)
    expect(Date.parse(params.to!)).toBe(to)
    expect(params.prev_from).toBeUndefined()
    expect(controller.compareText.value).toBeNull()

    // 超 90 天:拒绝,不再发请求
    const callsAfterSet = vi.mocked(getDashboardRealtime).mock.calls.length
    controller.setCustomRange([from - 91 * 24 * 3_600_000, to])
    expect(vi.mocked(getDashboardRealtime).mock.calls.length).toBe(callsAfterSet)

    // 清空:回退今日窗口并重拉
    controller.setCustomRange(null)
    expect(controller.activeWindow.value).toBe('today')
    wrapper.unmount()
  })

  it('筛选切换:整屏下推三参数', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)

    controller.setFilter({ model: 'qwen2.5-7b', tenantId: 3, apiKeyId: 7 })
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(getDashboardRealtime).toHaveBeenLastCalledWith(
      expect.objectContaining({ dim: 'model', model: 'qwen2.5-7b', tenant_id: 3, api_key_id: 7 }),
      expect.any(AbortSignal),
    )
    wrapper.unmount()
  })

  it('切窗时中止在途旧请求,旧响应不落盘', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(controller.statCards.value[0]?.value).toBe('612.4K')

    // 静默 tick 的 /realtime:1s 后才落盘「旧窗口数据」(calls=111K),且随 signal abort 即拒绝(模拟 http 层中断)
    const staleData = makeRealtime()
    staleData.kpi.calls = kpi(111_000)
    vi.mocked(getDashboardRealtime).mockImplementationOnce((_params, signal) =>
      new Promise<RealtimeData>((resolve, reject) => {
        const onAbort = () => reject(new DOMException('Aborted', 'AbortError'))
        if (signal?.aborted) {
          onAbort()
          return
        }
        signal?.addEventListener('abort', onAbort)
        setTimeout(() => resolve(staleData), 1000)
      }),
    )
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getDashboardRealtime).toHaveBeenCalledTimes(2)

    // 切窗:restart 中止在途 tick 并带新窗口重拉(快速返回 calls=999K)
    const freshData = makeRealtime()
    freshData.kpi.calls = kpi(999_000)
    vi.mocked(getDashboardRealtime).mockImplementationOnce(() => Promise.resolve(freshData))
    controller.setWindow('1h')
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(controller.statCards.value[0]?.value).toBe('999K')

    // 旧 tick 的 1s 计时此刻到期:已被 abort,旧响应不落盘,store 仍为新窗口数据
    await vi.advanceTimersByTimeAsync(1000)
    expect(controller.statCards.value[0]?.value).toBe('999K')
    expect(controller.activeWindow.value).toBe('1h')
    wrapper.unmount()
  })

  it('表格随 modelRows 注入(useTable setData 联动)', async () => {
    vi.useFakeTimers()
    let controller!: ReturnType<typeof useController>
    const wrapper = mount({
      setup: () => {
        controller = useController()
        return () => h('div', [h(controller.ModelTable)])
      },
    })
    await vi.advanceTimersByTimeAsync(0)
    // 真实渲染表格:注入的行可见(数据行 + 合计行),而非只断言 store
    await vi.waitFor(() => expect(wrapper.text()).toContain('qwen2.5-7b'))
    expect(wrapper.text()).toContain('合计')
    wrapper.unmount()
  })
})
