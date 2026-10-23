// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() })),
}))
vi.mock('vue-router', () => ({
  useRouter: () => routerMock,
}))
vi.mock('@/store/auth', () => ({
  useAuthStore: () => authMock,
}))
vi.mock('@/api/dashboard', () => ({
  getTenantDashboardScreen: vi.fn(),
}))

import { getTenantDashboardScreen, type TenantDashboardScreenData } from '@/api/dashboard'
import { useStore } from '@/views/console/useStore'
import { useController } from '@/views/console/useController'

const routerMock = { push: vi.fn() }
const authMock = { userInfo: { username: 'tester' } as { username: string } | null }

const store = useStore()

function makeScreen(overrides: Partial<TenantDashboardScreenData> = {}): TenantDashboardScreenData {
  return {
    calls: 12_345,
    active_models: 3,
    error_rate: 0.002,
    total_tokens: 9_800_000,
    avg_ttft_ms: 145,
    trend: [
      { date: '2026-09-04', call_count: 1800, error_rate: 0.001 },
      { date: '2026-09-05', call_count: 0, error_rate: 0 },
    ],
    models: [{ id: 1, name: 'DeepSeek-V3' }],
    recent_calls: [
      {
        model: 'qwen-max',
        input_tokens: 100,
        output_tokens: 200,
        latency_ms: 320,
        status: 'success',
        created_at: '2026-09-05T10:00:00Z',
      },
      {
        model: null,
        input_tokens: 1,
        output_tokens: 2,
        latency_ms: null,
        status: 'error',
        created_at: null,
      },
    ],
    balance: '234.500000',
    frozen_balance: '100.000000',
    available: '134.500000',
    month_consume: '89.200000',
    ...overrides,
  }
}

class ResizeObserverStub {
  observe = vi.fn()
  disconnect = vi.fn()
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  authMock.userInfo = { username: 'tester' }
  vi.mocked(getTenantDashboardScreen).mockResolvedValue(makeScreen())
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

// ---------- store ----------

describe('console useStore(/tenant/dashboard/screen 对接)', () => {
  it('fetchConsole 拉取后填充统计卡/趋势/模型/最近调用/账户卡', async () => {
    await store.fetchConsole()

    expect(getTenantDashboardScreen).toHaveBeenCalledWith(undefined, undefined)
    expect(store.statCards.value).toEqual([
      { label: '今日调用', value: '1.23万' },
      { label: '活跃模型', value: '3' },
      { label: '成功率', value: '99.8%' },
      { label: '平均延迟', value: '145ms' },
    ])

    expect(store.trendDays.value).toEqual(['9/4', '9/5'])
    expect(store.trendCalls.value).toEqual([1800, 0])

    expect(store.myModels.value).toEqual([{ id: 1, name: 'DeepSeek-V3' }])

    // 最近调用:空模型/空延迟兜底,error 结果标 error 色调
    expect(store.recentCalls.value[0]).toMatchObject({
      model: 'qwen-max',
      latency: '320ms',
      tone: 'success',
      input: 100,
      output: 200,
    })
    expect(store.recentCalls.value[1]).toMatchObject({ model: '—', latency: '—', tone: 'error' })

    // 账户卡:Decimal 去尾零 + 可用占比
    expect(store.wallet.value).toEqual({
      balance: '234.5',
      frozen: '100',
      available: '134.5',
      monthConsume: '89.2',
      availablePercent: 57,
    })
  })

  it('平均首帧为 null 显示 —;总余额为 0 时可用占比 0', async () => {
    vi.mocked(getTenantDashboardScreen).mockResolvedValue(
      makeScreen({ avg_ttft_ms: null, balance: '0.000000', available: '0.000000' }),
    )
    await store.fetchConsole()
    expect(store.statCards.value[3]).toEqual({ label: '平均延迟', value: '—' })
    expect(store.wallet.value?.availablePercent).toBe(0)
  })
})

// ---------- controller ----------

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

describe('console useController', () => {
  it('按时段生成问候语(凌晨/早上/中午/下午/晚上)', () => {
    vi.useFakeTimers()
    const cases: Array<[number, string]> = [
      [3, '凌晨好'],
      [9, '早上好'],
      [13, '中午好'],
      [16, '下午好'],
      [21, '晚上好'],
    ]
    for (const [hour, expected] of cases) {
      vi.setSystemTime(new Date(2026, 7, 14, hour))
      const { controller, wrapper } = mountController()
      expect(controller.greeting.value).toBe(expected)
      wrapper.unmount()
    }
    vi.useRealTimers()
  })

  it('username 来自 auth store,未登录为 undefined', () => {
    const { controller, wrapper } = mountController()
    expect(controller.username.value).toBe('tester')
    wrapper.unmount()

    authMock.userInfo = null
    const { controller: c2, wrapper: w2 } = mountController()
    expect(c2.username.value).toBeUndefined()
    w2.unmount()
  })

  it('快捷操作五项,goRoute 按 name 跳转', () => {
    const { controller, wrapper } = mountController()
    expect(controller.quickActions).toHaveLength(5)
    expect(controller.quickActions[0]).toMatchObject({ label: '创建API Key', name: 'api-key' })

    controller.goRoute('playground')
    expect(routerMock.push).toHaveBeenCalledWith({ name: 'playground' })
    wrapper.unmount()
  })

  it('加载完成后 loading 置 false', async () => {
    const { controller, wrapper } = mountController()
    expect(controller.loading.value).toBe(true)
    await vi.waitFor(() => expect(controller.loading.value).toBe(false))
    await nextTick()
    wrapper.unmount()
  })

  it('首屏加载失败也置 loading false(UR-002:骨架屏不因首请求失败永久停留)', async () => {
    vi.useFakeTimers()
    vi.mocked(getTenantDashboardScreen).mockRejectedValueOnce(new Error('network error'))
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    expect(controller.loading.value).toBe(false)
    // 失败后轮询链继续,下个 30s 周期自动重试
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getTenantDashboardScreen).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('定时刷新:加载完成后每 30s 静默拉一轮并重绘趋势图,卸载后停止', async () => {
    vi.useFakeTimers()
    const { controller, wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)
    expect(controller.loading.value).toBe(false)

    vi.mocked(getTenantDashboardScreen).mockClear()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getTenantDashboardScreen).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getTenantDashboardScreen).toHaveBeenCalledTimes(2)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(getTenantDashboardScreen).toHaveBeenCalledTimes(2)
  })

  it('定时刷新:标签页隐藏时暂停,回到前台立即刷新并恢复轮询', async () => {
    vi.useFakeTimers()
    const { wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)

    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    vi.mocked(getTenantDashboardScreen).mockClear()
    await vi.advanceTimersByTimeAsync(120_000)
    expect(getTenantDashboardScreen).not.toHaveBeenCalled()

    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(getTenantDashboardScreen).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(30_000)
    expect(getTenantDashboardScreen).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('定时刷新:单次失败静默跳过,下个周期重试', async () => {
    vi.useFakeTimers()
    const { wrapper } = mountController()
    await vi.advanceTimersByTimeAsync(0)

    vi.mocked(getTenantDashboardScreen).mockRejectedValueOnce(new Error('network error'))
    vi.mocked(getTenantDashboardScreen).mockClear()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(getTenantDashboardScreen).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(30_000)
    expect(getTenantDashboardScreen).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
