// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { usePolling } from '@/views/dashboard-shared/usePolling'

function mountPolling(fetcher: () => Promise<unknown>, intervalMs: number) {
  let api!: ReturnType<typeof usePolling>
  const wrapper = mount({
    setup: () => {
      api = usePolling(fetcher, intervalMs)
      return () => h('div')
    },
  })
  return { api, wrapper }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('usePolling', () => {
  it('不自动首发:链由首次 refresh() 开启(首屏 loading 由 controller 控制)', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn(async () => {})
    const { wrapper } = mountPolling(fetcher, 10_000)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetcher).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('refresh() 立即拉取并开启周期链,单链不重入', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn(async () => {})
    const { api, wrapper } = mountPolling(fetcher, 10_000)
    await api.refresh()
    expect(fetcher).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetcher).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetcher).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('单次失败静默重试,周期照常', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn().mockRejectedValueOnce(new Error('boom')).mockResolvedValue({})
    const { api, wrapper } = mountPolling(fetcher, 10_000)
    await api.refresh()
    expect(fetcher).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetcher).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('标签页隐藏暂停,回前台立即拉取并恢复周期', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn(async () => {})
    const { api, wrapper } = mountPolling(fetcher, 10_000)
    await api.refresh()

    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetcher).toHaveBeenCalledTimes(1)

    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(fetcher).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetcher).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('卸载后停止(组件卸载即停链)', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn(async () => {})
    const { api, wrapper } = mountPolling(fetcher, 10_000)
    await api.refresh()
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
