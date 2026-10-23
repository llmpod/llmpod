// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { mount } from '@vue/test-utils'

vi.mock('@/api/dashboard', () => ({
  getDashboardRealtime: vi.fn(),
  getDashboardEngineHistory: vi.fn(),
  getDashboardAnomaly: vi.fn(),
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

import {
  getDashboardAnomaly,
  getDashboardEngineHistory,
  getDashboardRealtime,
  type AnomalyData,
  type EngineHistoryData,
  type RealtimeData,
} from '@/api/dashboard'
import { anomalyBarColor, successRateTextClass, useStore } from '@/views/engine-dashboard/useStore'
import { useController } from '@/views/engine-dashboard/useController'

const store = useStore()

/** 取最近一次 /realtime 调用的 params */
function realtimeCall(): NonNullable<Parameters<typeof getDashboardRealtime>[0]> {
  return vi.mocked(getDashboardRealtime).mock.calls.at(-1)?.[0]!
}

type KpiRaw = { value: number | null; delta_pct?: number | null; delta?: number | null; delta_pp?: number | null; delta_ms?: number | null; peak?: number | null; instance_id?: string | null }

function kpi(value: number | null, extra: KpiRaw = {}): KpiRaw {
  return { value, delta_pct: null, delta: null, delta_pp: null, delta_ms: null, ...extra }
}

function makeRealtime(overrides: Partial<RealtimeData> = {}): RealtimeData {
  return {
    ts: '2026-09-15T14:30:00+08:00',
    window: { from: '', to: '', granularity: '30s', source: 'hot', age_ms: 4200 },
    kpi: {
      qps: kpi(null),
      tpm: kpi(null),
      calls: kpi(null),
      total_tokens_kpi: kpi(null),
      avg_context: kpi(1_800, { delta: 120 }),
      error_rate: kpi(0.0048, { delta_pp: 0.12 }),
      truncation_rate: kpi(null),
      output_tps: kpi(null),
      in_flight: kpi(37, { delta_pct: 12, peak: 61 }),
      p95_ms: kpi(null),
      cache_hit_rate: kpi(0.637, { delta_pp: 2.1 }),
      latency_avg_ms: kpi(1240, { delta_ms: 80 }),
      ttft_avg_ms: kpi(380, { delta_ms: 50 }),
      kv_cache_util: kpi(0.93, { instance_id: 'inst-2' }),
      waiting_requests: kpi(7),
    },
    trend: {
      baseline: { qpm: 100, tpm: 200 },
      points: [
        { ts: '2026-09-15T13:00:00+08:00', qpm: 100, tpm: 200, prompt_tpm: 150, completion_tpm: 50, requests: 600, latency_avg_ms: 1200 },
        { ts: '2026-09-15T13:05:00+08:00', qpm: 178, tpm: 260, prompt_tpm: 190, completion_tpm: 70, requests: 900, latency_avg_ms: 1100 },
      ],
    },
    composition: {
      dim: 'model',
      total: [
        { ts: '2026-09-15T13:00:00+08:00', tpm: 200 },
        { ts: '2026-09-15T13:05:00+08:00', tpm: 260 },
      ],
      items: [
        {
          key: 'qwen2.5-7b',
          tpm: [
            { ts: '2026-09-15T13:00:00+08:00', value: 150 },
            { ts: '2026-09-15T13:05:00+08:00', value: 180 },
          ],
          requests: [
            { ts: '2026-09-15T13:00:00+08:00', value: 500 },
            { ts: '2026-09-15T13:05:00+08:00', value: 600 },
          ],
        },
      ],
    },
    distribution: null,
    engines: [
      {
        instance_id: 'inst-0',
        deployment_id: 'dep-1',
        deployment_name: 'qwen2.5-7b',
        engine: 'vllm',
        rank: 0,
        status: 'READY',
        ok: true,
        kv_cache_util: 0.68,
        running_requests: 4,
        waiting_requests: 0,
        prefix_cache_hit_rate: 0.712,
        num_preemptions: 0,
        age_ms: 3000,
      },
      {
        instance_id: 'inst-2',
        deployment_id: 'dep-1',
        deployment_name: 'qwen2.5-7b',
        engine: 'vllm',
        rank: 2,
        status: 'READY',
        ok: true,
        kv_cache_util: 0.93,
        running_requests: 5,
        waiting_requests: 5,
        prefix_cache_hit_rate: 0.521,
        num_preemptions: 2,
        age_ms: 9000,
      },
      {
        instance_id: 'inst-9',
        deployment_id: 'dep-4',
        deployment_name: 'llama-3.1-8b',
        engine: 'llamacpp',
        rank: 0,
        status: 'RUNNING',
        // llama.cpp 恒 ok=false + 指标全 null(§3.3)
        ok: false,
        kv_cache_util: null,
        running_requests: null,
        waiting_requests: null,
        prefix_cache_hit_rate: null,
        num_preemptions: null,
        age_ms: null,
      },
    ],
    ...overrides,
  } as RealtimeData
}

function makeHistory(overrides: Partial<EngineHistoryData> = {}): EngineHistoryData {
  return {
    window: { from: '2026-09-15T13:00:00+08:00', to: '2026-09-15T14:00:00+08:00', granularity: '5m' },
    instances: [
      {
        instance_id: 'inst-0',
        deployment_id: 'dep-1',
        deployment_name: 'qwen2.5-7b',
        engine: 'vllm',
        rank: 0,
        status: 'READY',
        series: [
          { ts: '2026-09-15T13:00:00+08:00', kv_avg: 0.3, kv_max: 0.4, preemptions: 0, samples: 12 },
          { ts: '2026-09-15T13:05:00+08:00', kv_avg: 0.71, kv_max: 0.83, preemptions: 0, samples: 12 },
        ],
      },
      {
        instance_id: 'inst-2',
        deployment_id: 'dep-1',
        deployment_name: 'qwen2.5-7b',
        engine: 'vllm',
        rank: 2,
        status: 'READY',
        series: [
          // 13:00 桶缺失(samples=0 的桶不存在行)→ 空档,不补 0(§4.1)
          { ts: '2026-09-15T13:05:00+08:00', kv_avg: 0.83, kv_max: 0.9, preemptions: 1, samples: 11 },
        ],
      },
    ],
    ...overrides,
  }
}

function makeAnomaly(overrides: Partial<AnomalyData> = {}): AnomalyData {
  return {
    ts: '2026-09-15T14:30:00+08:00',
    window: { from: '2026-09-15T13:30:00+08:00', to: '2026-09-15T14:30:00+08:00' },
    anomaly: {
      total: 52,
      ratio: 2.1667,
      items: [
        { kind: 'other', count: 36, share: 0.6923, side: 'error' },
        { kind: 'upstream_4xx', count: 16, share: 0.3077, side: 'error' },
        { kind: 'rate_limited', count: 5, share: 0.09, side: 'rejected' },
      ],
    },
    upstream: {
      local: { success_rate: 0.9962, latency_avg_ms: 620, qpm: 112 },
      external: { success_rate: 0.916, latency_avg_ms: 1850, qpm: 16 },
      providers: [
        { name: 'xfyun', success_rate: 0.842, latency_avg_ms: 2600, qpm: 4 },
        { name: 'openai', success_rate: 0.984, latency_avg_ms: 1300, qpm: 6 },
      ],
    },
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  store.realtimeWindow.value = '1h'
  vi.mocked(getDashboardRealtime).mockResolvedValue(makeRealtime())
  vi.mocked(getDashboardEngineHistory).mockResolvedValue(makeHistory())
  vi.mocked(getDashboardAnomaly).mockResolvedValue(makeAnomaly())
})

afterEach(() => {
  vi.useRealTimers()
})

describe('engine-dashboard useStore', () => {
  it('fetchRealtime:KPI 八卡(在途含峰值、KV 附最忙实例、kind 按卡而用);参数为区间 + 上一时段对照窗', async () => {
    await store.fetchRealtime()

    const params = realtimeCall()
    expect(params.dim).toBe('model')
    // 2026-09-17 区间化:window/granularity 已下线(传了 422)
    expect(params.window).toBeUndefined()
    expect(params.granularity).toBeUndefined()
    // 1h 滚动窗:[now-1h, now];prev_* = [now-2h, now-1h](等长,紧邻)
    const from = Date.parse(params.from!)
    const to = Date.parse(params.to!)
    expect(to - from).toBe(3_600_000)
    expect(Date.parse(params.prev_from!)).toBe(from - 3_600_000)
    expect(Date.parse(params.prev_to!)).toBe(to - 3_600_000)

    const cards = store.kpiCards.value
    expect(cards.map(card => card.label)).toEqual([
      '在途峰值',
      'KV 水位',
      '排队',
      '平均上下文',
      '错误率',
      '首字延迟',
      '平均延迟',
      '缓存命中率',
    ])
    expect(cards[0]).toMatchObject({ value: '37 / 61', deltaText: '↑ 12% 较上一时段', deltaTone: 'bad' })
    expect(cards[1]).toMatchObject({ value: '93%', note: '最忙 qwen2.5-7b #2' })
    expect(cards[2]).toMatchObject({ value: '7' })
    expect(cards[3]).toMatchObject({ value: '1,800', deltaText: '↑ 120 较上一时段', deltaTone: null })
    expect(cards[4]).toMatchObject({ value: '0.48%', deltaText: '↑ 0.12pp 较上一时段', deltaTone: 'bad' })
    expect(cards[5]).toMatchObject({ value: '380ms' })
    expect(cards[6]).toMatchObject({ value: '1.24s' })
    expect(cards[7]).toMatchObject({ value: '63.7%', deltaText: '↑ 2.1pp 较上一时段', deltaTone: 'good' })
  })

  it('冷层 in_flight 双 null → — 且置灰(§5)', async () => {
    vi.mocked(getDashboardRealtime).mockResolvedValue(
      makeRealtime({
        kpi: { ...makeRealtime().kpi, in_flight: kpi(null) },
      }),
    )
    await store.fetchRealtime()
    expect(store.kpiCards.value[0]).toMatchObject({ value: '—', deltaText: null })
  })

  it('引擎网格:ok=false 渲染「指标不可用」而非 0;抢占>0 标记;年龄照实(§3.3)', async () => {
    await store.fetchRealtime()
    const rows = store.engineRows.value
    expect(rows).toHaveLength(3)
    expect(rows[0]).toMatchObject({
      name: 'qwen2.5-7b',
      rank: 0,
      ok: true,
      kvPct: 0.68,
      kvText: '68%',
      waiting: '0',
      running: '4',
      hitText: '71.2%',
      preemptionAlert: false,
      ageText: '3s 前',
    })
    expect(rows[1]).toMatchObject({ kvText: '93%', preemptions: 2, preemptionAlert: true })
    expect(rows[2]).toMatchObject({ ok: false, kvPct: null, kvText: '—', hitText: '—', ageText: '—' })
  })

  it('吞吐趋势:qpm/tpm 序列 + baseline 可索引判断(§3.2)', async () => {
    await store.fetchRealtime()
    const throughput = store.throughput.value!
    expect(throughput.labels).toEqual(['13:00', '13:05'])
    expect(throughput.qpm).toEqual([100, 178])
    expect(throughput.tpm).toEqual([200, 260])
    expect(throughput.canIndex).toBe(true)
    expect(throughput.baselineQpm).toBe(100)
  })

  it('关注横幅:KV 饱和(>0.9)实例 + 外部上游低成功率,前端自算(§8 #9)', async () => {
    await store.fetchRealtime()
    await store.fetchAnomaly()
    const banner = store.banner.value
    expect(banner.ok).toBe(false)
    expect(banner.items.some(text => text.includes('qwen2.5-7b #2'))).toBe(true)
    expect(banner.items.some(text => text.includes('xfyun'))).toBe(true)
  })

  it('关注横幅:anomaly 先落地,路由重进(第二次 useStore)后 realtime 尾部仍保留两类条目', async () => {
    await store.fetchAnomaly()
    const reentered = useStore() // 路由重进:新调用方,模块级快照仍在(I-1:闭包版此处为空)
    await reentered.fetchRealtime()
    const banner = reentered.banner.value
    expect(banner.ok).toBe(false)
    expect(banner.items.some(text => text.includes('qwen2.5-7b #2'))).toBe(true) // KV 饱和(realtime 尾部重算)
    expect(banner.items.some(text => text.includes('xfyun'))).toBe(true) // 上游条目(anomaly 的 last-good 快照)
  })

  it('fetchHistory:稀疏格对齐成矩阵,缺格为 null 空档(§4.1);history 改传区间参数(1h 词表 → 1h 跨度)', async () => {
    await store.fetchHistory()
    const historyCall = vi.mocked(getDashboardEngineHistory).mock.calls.at(-1)?.[0]
    // 2026-09-17 区间化:window 已下线,改传 from/to
    expect(historyCall).toMatchObject({ from: expect.any(String), to: expect.any(String) })
    expect(historyCall?.window).toBeUndefined()
    expect(Date.parse(historyCall!.to) - Date.parse(historyCall!.from)).toBe(3_600_000)
    const heatmap = store.heatmap.value!
    // granularity=5m → HH:mm 标签
    expect(heatmap.labels).toEqual(['13:00', '13:05'])
    expect(heatmap.rows).toHaveLength(2)
    expect(heatmap.rows[0]).toMatchObject({ label: 'qwen2.5-7b #0' })
    expect(heatmap.rows[0]!.cells[0]).toMatchObject({ kvAvg: 0.3, kvMax: 0.4, preemptions: 0 })
    expect(heatmap.rows[1]!.cells[0]).toBeNull()
    expect(heatmap.rows[1]!.cells[1]).toMatchObject({ kvAvg: 0.83, preemptions: 1 })
  })

  it('fetchAnomaly:ratio 只做文本(可 >1)、kind 中文标签、side 区分(§4.2);anomaly 改传区间参数', async () => {
    await store.fetchAnomaly()
    const anomalyCall = vi.mocked(getDashboardAnomaly).mock.calls.at(-1)?.[0]
    expect(anomalyCall).toMatchObject({ from: expect.any(String), to: expect.any(String) })
    expect(anomalyCall?.window).toBeUndefined()
    const anomaly = store.anomalyView.value!
    expect(anomaly.totalText).toBe('52')
    expect(anomaly.ratioText).toBe('216.7%')
    expect(anomaly.items[0]).toMatchObject({ label: '其他', count: '36', sideText: '上游错误', side: 'error' })
    expect(anomaly.items[1]).toMatchObject({ label: '上游 4xx', sideText: '上游错误', side: 'error' })
    expect(anomaly.items[2]).toMatchObject({ label: '限流命中', sideText: '准入拒绝', side: 'rejected' })
  })

  it('异常构成条色按归属:准入拒绝 warning / 上游错误(含未知值兜底)error', () => {
    expect(anomalyBarColor('rejected')).toBe('rgb(var(--n-warning-color))')
    expect(anomalyBarColor('error')).toBe('rgb(var(--n-error-color))')
    expect(anomalyBarColor('')).toBe('rgb(var(--n-error-color))')
  })

  it('上游健康:local/external/providers 卡,低成功率标关注,并保留原始成功率供分档着色', async () => {
    await store.fetchAnomaly()
    const upstream = store.upstreamHealth.value!
    expect(upstream.local).toMatchObject({ name: 'local', successText: '99.62%', latencyText: '620ms', qpmText: '112', attention: false, successRate: 0.9962 })
    expect(upstream.external).toMatchObject({ name: 'external', successText: '91.6%', attention: false, successRate: 0.916 })
    expect(upstream.providers[0]).toMatchObject({ name: 'xfyun', successText: '84.2%', attention: true, successRate: 0.842 })
  })

  it('成功率无样本(null):文本 — 且不参与分档着色', async () => {
    vi.mocked(getDashboardAnomaly).mockResolvedValue(
      makeAnomaly({
        upstream: {
          local: { success_rate: null, latency_avg_ms: 620, qpm: 112 },
          external: null,
          providers: [],
        },
      }),
    )
    await store.fetchAnomaly()
    const upstream = store.upstreamHealth.value!
    expect(upstream.local).toMatchObject({ successText: '—', attention: false, successRate: null })
    expect(upstream.external).toBeNull()
  })

  it('成功率分档配色:≥99% 绿、95–99% 中性、90–95% 橙、<90% 红;无数据不着色', () => {
    expect(successRateTextClass(1)).toBe('text-success-text')
    expect(successRateTextClass(0.99)).toBe('text-success-text')
    expect(successRateTextClass(0.9899)).toBe('')
    expect(successRateTextClass(0.95)).toBe('')
    expect(successRateTextClass(0.9499)).toBe('text-warning-text')
    expect(successRateTextClass(0.9)).toBe('text-warning-text')
    expect(successRateTextClass(0.8999)).toBe('text-error-text')
    expect(successRateTextClass(0)).toBe('text-error-text')
    expect(successRateTextClass(null)).toBe('')
  })

  it('三路共用全屏唯一窗口:history/anomaly 与 realtime 同区间(24h 窗 → 24h 跨度)', async () => {
    store.realtimeWindow.value = '24h'
    await store.fetchRealtime()
    await store.fetchHistory()
    await store.fetchAnomaly()
    const realtimeParams = realtimeCall()
    expect(realtimeParams.dim).toBe('model')
    expect(Date.parse(realtimeParams.to!) - Date.parse(realtimeParams.from!)).toBe(24 * 3_600_000)
    const historyCall = vi.mocked(getDashboardEngineHistory).mock.calls.at(-1)![0]!
    const anomalyCall = vi.mocked(getDashboardAnomaly).mock.calls.at(-1)![0]!
    for (const call of [historyCall, anomalyCall]) {
      expect(Date.parse(call.to) - Date.parse(call.from)).toBe(24 * 3_600_000)
      // 热力图/异常不带对照窗(只传 from/to)
      expect('prev_from' in call).toBe(false)
    }
  })
})

describe('engine-dashboard useController', () => {
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

  it('三路并行首发:realtime/history/anomaly 各一次,加载标志分别落定', async () => {
    const { controller, wrapper } = mountController()
    expect(controller.realtimeLoading.value).toBe(true)
    await vi.waitFor(() => {
      expect(controller.realtimeLoading.value).toBe(false)
      expect(controller.historyLoading.value).toBe(false)
      expect(controller.anomalyLoading.value).toBe(false)
    })
    expect(getDashboardRealtime).toHaveBeenCalledTimes(1)
    expect(getDashboardEngineHistory).toHaveBeenCalledTimes(1)
    expect(getDashboardAnomaly).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('三路轮询节奏:realtime 10s / anomaly 30s / history 60s(§0)', async () => {
    vi.useFakeTimers()
    const { wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    vi.mocked(getDashboardRealtime).mockClear()
    vi.mocked(getDashboardEngineHistory).mockClear()
    vi.mocked(getDashboardAnomaly).mockClear()

    await vi.advanceTimersByTimeAsync(10_000)
    expect(getDashboardRealtime).toHaveBeenCalledTimes(1)
    expect(getDashboardAnomaly).toHaveBeenCalledTimes(0)
    expect(getDashboardEngineHistory).toHaveBeenCalledTimes(0)

    await vi.advanceTimersByTimeAsync(20_000) // t=30s
    expect(getDashboardRealtime).toHaveBeenCalledTimes(3)
    expect(getDashboardAnomaly).toHaveBeenCalledTimes(1)
    expect(getDashboardEngineHistory).toHaveBeenCalledTimes(0)

    await vi.advanceTimersByTimeAsync(30_000) // t=60s
    expect(getDashboardEngineHistory).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('窗口切换三路联动:页头唯一窗口同时重拉 realtime + history + anomaly', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(controller.historyLoading.value).toBe(false))

    controller.setRealtimeWindow('24h')
    await vi.waitFor(() => {
      expect(controller.realtimeLoading.value).toBe(false)
      expect(controller.historyLoading.value).toBe(false)
      expect(controller.anomalyLoading.value).toBe(false)
    })
    const realtimeParams = realtimeCall()
    expect(realtimeParams.dim).toBe('model')
    expect(Date.parse(realtimeParams.to!) - Date.parse(realtimeParams.from!)).toBe(24 * 3_600_000)
    // 热力图/异常联动重拉,与 realtime 同区间
    expect(getDashboardEngineHistory).toHaveBeenCalledTimes(2)
    expect(getDashboardEngineHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({ from: expect.any(String), to: expect.any(String) }),
      expect.any(AbortSignal),
    )
    expect(getDashboardAnomaly).toHaveBeenCalledTimes(2)
    expect(getDashboardAnomaly).toHaveBeenLastCalledWith(
      expect.objectContaining({ from: expect.any(String), to: expect.any(String) }),
      expect.any(AbortSignal),
    )
    expect(controller.realtimeWindowLabel.value).toBe('近24小时')
    wrapper.unmount()
  })

  it('吞吐趋势索引化:canIndex 时可切换,基准 = 首槽(§3.2)', async () => {
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(controller.realtimeLoading.value).toBe(false))
    expect(controller.canIndex.value).toBe(true)
    expect(controller.indexed.value).toBe(false)
    controller.toggleIndexed(true)
    expect(controller.indexed.value).toBe(true)
    wrapper.unmount()
  })

  it('连点切窗 seq 守卫:被取代的轮中止后不提前收尾,按末轮单次收尾', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(controller.historyLoading.value).toBe(false))

    // 'today' 轮 history 请求挂起且随 signal abort 即拒绝(模拟 http 层中断,旧参数响应不落盘)
    vi.mocked(getDashboardEngineHistory).mockImplementationOnce((_params, signal) =>
      new Promise<EngineHistoryData>((_, reject) => {
        const onAbort = () => reject(new DOMException('Aborted', 'AbortError'))
        if (signal?.aborted) {
          onAbort()
          return
        }
        signal?.addEventListener('abort', onAbort)
      }),
    )
    // '24h' 轮 history 请求受控:测试手动 resolve,便于在 today 轮被中止后、24h 轮完成前观测 loading
    let resolveFresh!: (data: EngineHistoryData) => void
    vi.mocked(getDashboardEngineHistory).mockImplementationOnce(() =>
      new Promise<EngineHistoryData>((resolve) => {
        resolveFresh = resolve
      }),
    )

    controller.setRealtimeWindow('today')
    expect(controller.historyLoading.value).toBe(true) // 回骨架
    controller.setRealtimeWindow('24h') // 连点:中止 today 在途轮,接管收尾权

    await vi.advanceTimersByTimeAsync(0) // today 轮的 abort 拒绝链此刻落定
    expect(controller.historyLoading.value).toBe(true) // seq 守卫:被取代的轮不得提前收尾(否则骨架提前消失)

    resolveFresh(makeHistory()) // 24h 轮响应返回
    await vi.waitFor(() => expect(controller.historyLoading.value).toBe(false))
    expect(getDashboardEngineHistory).toHaveBeenCalledTimes(3) // 首发 1h + today + 24h,各一次
    expect(getDashboardEngineHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({ from: expect.any(String), to: expect.any(String) }),
      expect.any(AbortSignal),
    )
    expect(controller.realtimeWindow.value).toBe('24h')
    wrapper.unmount()
  })

  it('切窗中止静默 tick 在途旧请求:旧响应不落盘,store 保持 24h 新轮数据', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(controller.realtimeLoading.value).toBe(false))
    expect(controller.kpiCards.value[0]?.value).toBe('37 / 61') // 首轮已落盘

    // 静默 tick 的 /realtime:1s 后才返回旧窗口数据(在途 111),随 signal abort 即拒绝(模拟 http 层中断)
    const staleData = makeRealtime()
    staleData.kpi.in_flight = kpi(111, { peak: 222 })
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
    await vi.advanceTimersByTimeAsync(10_000) // t=10s:静默 tick 发起,请求在途
    expect(getDashboardRealtime).toHaveBeenCalledTimes(2)

    // 切窗:restart 中止在途 tick 并带新窗口重拉(快速返回在途 999)
    const freshData = makeRealtime()
    freshData.kpi.in_flight = kpi(999, { peak: 888 })
    vi.mocked(getDashboardRealtime).mockImplementationOnce(() => Promise.resolve(freshData))
    controller.setRealtimeWindow('24h')
    await vi.waitFor(() => expect(controller.realtimeLoading.value).toBe(false))
    const realtimeParams = realtimeCall()
    expect(realtimeParams.dim).toBe('model')
    expect(Date.parse(realtimeParams.to!) - Date.parse(realtimeParams.from!)).toBe(24 * 3_600_000)
    expect(controller.kpiCards.value[0]?.value).toBe('999 / 888')

    // 旧 tick 的 1s 计时此刻到期:已被 abort,旧响应不落盘,store 仍为新窗口数据
    await vi.advanceTimersByTimeAsync(1000)
    expect(controller.kpiCards.value[0]?.value).toBe('999 / 888')
    expect(controller.realtimeLoading.value).toBe(false)
    expect(controller.realtimeWindow.value).toBe('24h')
    wrapper.unmount()
  })
})
