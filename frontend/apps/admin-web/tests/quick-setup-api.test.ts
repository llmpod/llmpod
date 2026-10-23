// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/cluster', () => ({ listClusters: vi.fn() }))
// request 链路(http/theme/message)与缓存语义无关,mock 掉避免拖慢
vi.mock('@/api/request', () => ({ request: vi.fn() }))

import { listClusters } from '@/api/cluster'

async function freshModule() {
  vi.resetModules()
  vi.doMock('@/api/cluster', () => ({ listClusters: vi.mocked(listClusters) }))
  vi.doMock('@/api/request', () => ({ request: vi.fn() }))
  return import('@/api/quick-setup')
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
})

describe('quick-setup api 集群存在性缓存', () => {
  it('首次探测后缓存,同一会话不再请求', async () => {
    vi.mocked(listClusters).mockResolvedValue({ total: 3 } as Awaited<ReturnType<typeof listClusters>>)
    const mod = await freshModule()

    expect(await mod.hasAnyCluster()).toBe(true)
    expect(await mod.hasAnyCluster()).toBe(true)
    expect(listClusters).toHaveBeenCalledTimes(1)
    expect(listClusters).toHaveBeenCalledWith({ page: 1, page_size: 1 })
  })

  it('空集群探测为 false 并缓存', async () => {
    vi.mocked(listClusters).mockResolvedValue({ total: 0 } as Awaited<ReturnType<typeof listClusters>>)
    const mod = await freshModule()
    expect(await mod.hasAnyCluster()).toBe(false)
    expect(await mod.hasAnyCluster()).toBe(false)
    expect(listClusters).toHaveBeenCalledTimes(1)
  })

  it('markClusterCreated 直接置缓存为 true(不再请求)', async () => {
    const mod = await freshModule()
    mod.markClusterCreated()
    expect(await mod.hasAnyCluster()).toBe(true)
    expect(listClusters).not.toHaveBeenCalled()
    expect(mod.mockClusterState.hasCluster).toBe(true)
  })

  it('resetClusterCache 失效缓存并清除完成标记,下次重新探测', async () => {
    vi.mocked(listClusters).mockResolvedValue({ total: 0 } as Awaited<ReturnType<typeof listClusters>>)
    const mod = await freshModule()
    sessionStorage.setItem(mod.QUICK_SETUP_DONE_KEY, '1')
    mod.markClusterCreated()

    mod.resetClusterCache()
    expect(sessionStorage.getItem(mod.QUICK_SETUP_DONE_KEY)).toBeNull()
    expect(mod.mockClusterState.hasCluster).toBe(false)

    expect(await mod.hasAnyCluster()).toBe(false)
    expect(listClusters).toHaveBeenCalledTimes(1) // 重新探测
  })

  it('跳过/完成标记 key 约定', async () => {
    const mod = await freshModule()
    expect(mod.QUICK_SETUP_SKIP_KEY).toBe('llmpod:quick-setup:skipped')
    expect(mod.QUICK_SETUP_DONE_KEY).toBe('llmpod:quick-setup:done')
  })
})
