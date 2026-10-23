// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { getDashboardOverview, type OverviewData } from '@/api/dashboard'
import { useStore } from '@/views/dashboard/useStore'
import { useController } from '@/views/dashboard/useController'
import DashboardView from '@/views/dashboard/index.vue'

vi.mock('@/api/dashboard', () => ({
  getDashboardOverview: vi.fn(),
}))

const { routerPush } = vi.hoisted(() => ({ routerPush: vi.fn() }))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}))

const { hasMenuMock } = vi.hoisted(() => ({ hasMenuMock: vi.fn<(name: string) => boolean>() }))

vi.mock('@/store/auth', () => ({
  useAuthStore: () => ({ hasMenu: hasMenuMock }),
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

function makeOverview(overrides: Partial<OverviewData> = {}): OverviewData {
  return {
    ts: '2026-09-15T14:39:59+08:00',
    window: { from: '2026-09-15T00:00:00+08:00', to: '2026-09-15T14:39:59+08:00' },
    kpi: {
      calls: { value: 128_400, delta_pct: 12.0, delta: null, delta_pp: null, delta_ms: null, week_total: null },
      active_tenants: { value: 38, delta_pct: null, delta: 2, delta_pp: null, delta_ms: null, week_total: null },
      active_models: { value: 12, delta_pct: null, delta: 1, delta_pp: null, delta_ms: null, week_total: null },
      success_rate: { value: 0.9924, delta_pct: null, delta: null, delta_pp: -0.4, delta_ms: null, week_total: null },
      avg_ttft_ms: { value: 610, delta_pct: null, delta: null, delta_pp: null, delta_ms: -20, week_total: null },
      new_tenants: { value: 3, delta_pct: null, delta: null, delta_pp: null, delta_ms: null, week_total: 8 },
    },
    trend: {
      points: [
        { hour: 9, calls: 3, calls_yesterday: 1 },
        { hour: 12, calls: 34, calls_yesterday: 0 },
      ],
      today_total: 64,
      yesterday_same_period_total: 5,
    },
    upstream: {
      local: { share: 0.812, success_rate: 0.9962 },
      external: { share: 0.188, success_rate: 0.916 },
      providers: [
        { name: 'xfyun', share: 0.421, success_rate: 0.842 },
        { name: 'openai', share: 0.314, success_rate: 0.984 },
      ],
    },
    service: {
      online_models: 12,
      replicas: 34,
      engine_mix: { vllm: 24, sglang: 8, llamacpp: 2 },
      deployments: { running: 18, creating: 2, stopped: 0, failed: 1 },
      recent_deployments: [
        { id: 'dep1', name: 'DeepSeek-V3', status: 'running' },
        { id: 'dep2', name: 'Qwen3.5-Max', status: 'creating' },
      ],
      nodes: { online: 12, total: 12 },
      gpu: { online: 34, total: 48 },
      clusters: [
        {
          id: 1,
          name: 'gpu-shared',
          node_count: 4,
          online_node_count: 3,
          gpu_count: 18,
          gpu_utilization: 57.5,
          used_vram_gb: 640.2,
          total_vram_gb: 1440.0,
        },
        {
          id: 2,
          name: 'gpu-dedicated',
          node_count: 2,
          online_node_count: 2,
          gpu_count: 8,
          gpu_utilization: null,
          used_vram_gb: null,
          total_vram_gb: null,
        },
      ],
    },
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getDashboardOverview).mockResolvedValue(makeOverview())
  hasMenuMock.mockReturnValue(true)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('dashboard useStore(/admin/dashboard/overview 对接)', () => {
  it('fetchOverview 填充 KPI 六卡:四种 delta 量纲 + 新增租户本周累计', async () => {
    await store.fetchOverview()

    expect(getDashboardOverview).toHaveBeenCalledWith(undefined)
    const cards = store.kpiCards.value
    expect(cards.map(card => card.label)).toEqual([
      '调用量 · 今日',
      '活跃租户',
      '活跃模型',
      '新增租户',
      '成功率 · 今日',
      '平均首帧 · 今日',
    ])
    // §1.2:delta_pct 已是百分比、delta_pp 已是百分点(不再 ×100)、value 比率要 ×100
    expect(cards[0]).toMatchObject({ value: '128.4K', deltaText: '↑ 12% vs 昨日', deltaTone: 'good' })
    expect(cards[1]).toMatchObject({ value: '38', deltaText: '↑ 2 vs 昨日', deltaTone: 'good' })
    // 成功率下降 = 变坏 → bad;首帧延迟下降 = 变好 → good
    expect(cards[4]).toMatchObject({ value: '99.24%', deltaText: '↓ 0.4pp vs 昨日', deltaTone: 'bad' })
    expect(cards[5]).toMatchObject({ value: '610ms', deltaText: '↓ 20ms vs 昨日', deltaTone: 'good' })
    expect(cards[3]).toMatchObject({ value: '3', deltaText: null, note: '本周累计 8 家' })
  })

  it('无样本 → — 且隐藏环比(§1.1)', async () => {
    vi.mocked(getDashboardOverview).mockResolvedValue(
      makeOverview({
        kpi: {
          ...makeOverview().kpi,
          calls: { value: null, delta_pct: null, delta: null, delta_pp: null, delta_ms: null, week_total: null },
          new_tenants: { value: 2, delta_pct: null, delta: null, delta_pp: null, delta_ms: null, week_total: null },
        },
      }),
    )
    await store.fetchOverview()
    expect(store.kpiCards.value[0]).toMatchObject({ value: '—', deltaText: null })
    expect(store.kpiCards.value[3]!.note).toBeNull()
  })

  it('趋势:稀疏小时 0-23 连续铺、缺小时补 0、合计文案', async () => {
    await store.fetchOverview()
    const trend = store.trendView.value
    expect(trend.labels).toHaveLength(24)
    expect(trend.labels[9]).toBe('09:00')
    expect(trend.today[9]).toBe(3)
    expect(trend.today[10]).toBe(0)
    expect(trend.yesterday[12]).toBe(0)
    expect(store.trendTotalsText.value).toBe('今日 64 · 昨日同期 5')
  })

  it('上游构成:share/success 格式化,低成功率标关注(阈值 0.9)', async () => {
    await store.fetchOverview()
    const upstream = store.upstreamCards.value!
    expect(upstream.local).toMatchObject({ name: '本地推理', shareText: '81.2%', successText: '99.62%', attention: false })
    expect(upstream.external).toMatchObject({ shareText: '18.8%', successText: '91.6%', attention: false })
    expect(upstream.providers[0]).toMatchObject({ name: 'xfyun', shareText: '42.1%', attention: true })
    expect(upstream.providers[1]).toMatchObject({ name: 'openai', attention: false })
  })

  it('健康横幅:全健康 ok;节点/GPU 降级或部署失败 → 摘要', async () => {
    await store.fetchOverview()
    // fixture:GPU 34/48 降级 + failed=1
    expect(store.banner.value.ok).toBe(false)
    expect(store.banner.value.items).toContain('GPU 34/48 在线')
    expect(store.banner.value.items).toContain('1 个部署失败')

    vi.mocked(getDashboardOverview).mockResolvedValue(
      makeOverview({
        service: {
          ...makeOverview().service,
          gpu: { online: 48, total: 48 },
          deployments: { running: 18, creating: 2, stopped: 0, failed: 0 },
        },
        upstream: {
          ...makeOverview().upstream,
          external: { share: 0.188, success_rate: 0.95 },
        },
      }),
    )
    await store.fetchOverview()
    expect(store.banner.value).toEqual({ ok: true, items: [] })
  })

  it('服务面:规模/引擎分布/节点 GPU 在线/集群池/最近部署(跳转交互数据保留)', async () => {
    await store.fetchOverview()
    expect(store.serviceInfo.value).toEqual({
      onlineModels: 12,
      replicas: 34,
      engineMix: [
        { engine: 'vllm', count: 24 },
        { engine: 'sglang', count: 8 },
        { engine: 'llamacpp', count: 2 },
      ],
    })
    expect(store.nodesInfo.value).toEqual({ online: 12, total: 12, ok: true, percent: 100 })
    expect(store.gpuInfo.value).toEqual({ online: 34, total: 48, ok: false, percent: 71 })

    const pool = store.gpuPools.value[0]!
    expect(pool).toMatchObject({ id: 1, name: 'gpu-shared', gpuCount: 18, percent: 57.5, hot: false })
    expect(pool.meta).toContain('3/4 节点')
    expect(pool.meta).toContain('640.2/1440GB')
    expect(store.gpuPools.value[1]!.percent).toBeNull()
    expect(store.gpuPools.value[1]!.meta).toContain('显存 —')

    expect(store.deploySummary.value.map(item => item.status)).toEqual([
      'running',
      'creating',
      'stopped',
      'failed',
    ])
    expect(store.deployList.value[0]).toMatchObject({ name: 'DeepSeek-V3', statusLabel: '运行中', statusType: 'success' })
  })
})

describe('dashboard useController', () => {
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

  it('首轮拉取完成后 loading 置 false', async () => {
    const { controller, wrapper } = mountController()
    expect(controller.loading.value).toBe(true)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    wrapper.unmount()
  })

  it('30s 静默轮询,卸载后停止(§0:屏1 30s)', async () => {
    vi.useFakeTimers()
    const { wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    expect(getDashboardOverview).toHaveBeenCalledTimes(1)

    vi.mocked(getDashboardOverview).mockClear()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getDashboardOverview).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getDashboardOverview).toHaveBeenCalledTimes(2)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(getDashboardOverview).toHaveBeenCalledTimes(2)
  })

  it('标签页隐藏暂停,回到前台立即刷新并恢复轮询', async () => {
    vi.useFakeTimers()
    const { wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)

    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    vi.mocked(getDashboardOverview).mockClear()
    await vi.advanceTimersByTimeAsync(120_000)
    expect(getDashboardOverview).not.toHaveBeenCalled()

    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(getDashboardOverview).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('单次失败静默跳过,下个周期重试', async () => {
    vi.useFakeTimers()
    const { wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)

    vi.mocked(getDashboardOverview).mockRejectedValueOnce(new Error('network error'))
    vi.mocked(getDashboardOverview).mockClear()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getDashboardOverview).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getDashboardOverview).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('跳转与权限开关:部署/集群入口按 hasMenu 过滤', async () => {
    hasMenuMock.mockImplementation((name: string) => name !== 'deploy')
    const { controller, wrapper } = mountController()
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    expect(controller.canDeploy.value).toBe(false)
    expect(controller.canNodePool.value).toBe(true)

    controller.goDeployStatus('running')
    expect(routerPush).toHaveBeenCalledWith({ name: 'deploy', query: { action: 'status', status: 'running' } })
    controller.goDeployDetail('dep1')
    expect(routerPush).toHaveBeenCalledWith({ name: 'deploy', query: { action: 'detail', id: 'dep1' } })
    controller.goClusterDetail(2)
    expect(routerPush).toHaveBeenCalledWith({ name: 'node-pool', query: { topology: '2' } })
    wrapper.unmount()
  })

  it('视图冒烟:横幅/KPI/上游/服务面渲染;无权限时跳转入口隐藏', async () => {
    hasMenuMock.mockReturnValue(true)
    vi.mocked(getDashboardOverview).mockResolvedValue(makeOverview())
    const wrapper = mount(DashboardView, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('128.4K'))
    expect(document.body.textContent).toContain('2 项需关注') // GPU 34/48 + 失败部署 1
    expect(document.body.textContent).toContain('gpu-shared')
    expect(document.body.textContent).toContain('xfyun')
    expect(document.querySelectorAll('.dash-pool--link').length).toBe(2)
    expect(document.querySelectorAll('.dash-tile--link').length).toBe(4)
    wrapper.unmount()

    hasMenuMock.mockImplementation((name: string) => name !== 'deploy' && name !== 'node-pool')
    const wrapper2 = mount(DashboardView, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('gpu-shared'))
    expect(document.querySelectorAll('.dash-pool--link').length).toBe(0)
    expect(document.querySelectorAll('.dash-tile--link').length).toBe(0)
    wrapper2.unmount()
  })
})
