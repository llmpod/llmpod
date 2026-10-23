import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/node', () => ({
  listNodes: vi.fn(),
  getNode: vi.fn(),
  updateNode: vi.fn(),
  deleteNode: vi.fn(),
}))
vi.mock('@/api/gpu', () => ({ listGpus: vi.fn() }))

import { deleteNode, getNode, listNodes, updateNode } from '@/api/node'
import { listGpus } from '@/api/gpu'
import {
  formatDateTime,
  formatGbPair,
  formatPercent,
  gpuStatusMap,
  gpuSummary,
  nodeStatusMap,
  progressColor,
  useStore,
} from '@/views/node-list/useStore'

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listNodes).mockResolvedValue({
    items: [
      {
        id: 'n1',
        hostname: 'gpu-01',
        ip: '10.0.0.1',
        status: 'online',
        memory_gb: 64,
        runtime: {
          cpu_percent: 12.3,
          memory_used_gb: 45.6,
          memory_percent: 62.1,
          disk_total_gb: 500,
          disk_used_gb: 120,
          disk_percent: 55,
          gpu_count: 2,
          gpu_utilization: 88,
          used_vram_gb: 40.2,
          total_vram_gb: 80,
        },
      },
      { id: 'n2', hostname: 'cpu-01', ip: '10.0.0.2', status: 'offline', runtime: null },
    ],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listNodes>>)
  vi.mocked(listGpus).mockResolvedValue({
    items: [
      {
        id: 2,
        uuid: 'g2',
        node_id: 'n1',
        index: 1,
        name: 'A100',
        total_vram_gb: 80,
        driver_version: '570',
        status: 'online',
        runtime: {
          used_vram_gb: 40.2,
          memory_utilization: 50.1,
          utilization: 88.6,
          temperature: 72,
          fan_speed: 60,
          power_draw_w: 300.5,
          power_limit_w: 400,
          sm_clock_mhz: 1410,
          mem_clock_mhz: 1215,
          process_count: 3,
        },
      },
      { id: 1, uuid: 'g1', node_id: 'n1', index: 0, name: 'A100', total_vram_gb: 80, driver_version: '570', status: 'online', runtime: null },
    ],
    total: 2,
    page: 1,
    page_size: 100,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listGpus>>)
})

describe('node-list useStore', () => {
  it('request 按节点聚合 GPU(index 升序),无 GPU 节点为空数组', async () => {
    const result = await store.request({ page: 1, pageSize: 20 })
    const n1 = result.data.find((row) => row.id === 'n1')!
    expect(n1.gpus.map((gpu) => gpu.index)).toEqual([0, 1])
    const n2 = result.data.find((row) => row.id === 'n2')!
    expect(n2.gpus).toEqual([])
    expect(listNodes).toHaveBeenCalledWith({ page: 1, page_size: 20 }, undefined)
    expect(listGpus).toHaveBeenCalledWith({ page: 1, page_size: 100 }, undefined)
  })

  it('request 透传单卡 runtime 供展开行渲染:在线卡为对象,未上报卡为 null', async () => {
    const result = await store.request({ page: 1, pageSize: 20 })
    const n1 = result.data.find((row) => row.id === 'n1')!
    expect(n1.gpus.find((gpu) => gpu.index === 1)!.runtime).toMatchObject({
      utilization: 88.6,
      used_vram_gb: 40.2,
    })
    expect(n1.gpus.find((gpu) => gpu.index === 0)!.runtime).toBeNull()
  })

  it('request 透传 runtime:在线节点为对象,离线节点为 null', async () => {
    const result = await store.request({ page: 1, pageSize: 20 })
    const n1 = result.data.find((row) => row.id === 'n1')!
    expect(n1.runtime).toMatchObject({ cpu_percent: 12.3, gpu_count: 2, used_vram_gb: 40.2 })
    const n2 = result.data.find((row) => row.id === 'n2')!
    expect(n2.runtime).toBeNull()
  })

  it('keyword 仅过滤当前页(hostname/ip,大小写不敏感)', async () => {
    const result = await store.request({ page: 1, pageSize: 20, keyword: 'GPU-01' })
    expect(result.data).toHaveLength(1)
    expect(result.data[0].id).toBe('n1')
  })

  it('page_size 字段兼容,AbortSignal 透传', async () => {
    const controller = new AbortController()
    await store.request({ page: 2, pageSize: 20, page_size: 50 } as { page: number; pageSize: number }, { signal: controller.signal })
    expect(listNodes).toHaveBeenCalledWith({ page: 2, page_size: 50 }, controller.signal)
  })

  it('listNodeGpus 按 index 排序', async () => {
    const gpus = await store.listNodeGpus('n1')
    expect(gpus.map((gpu) => gpu.index)).toEqual([0, 1])
    expect(listGpus).toHaveBeenCalledWith({ node_id: 'n1', page: 1, page_size: 100 })
  })

  it('toggleMaintenance:online↔maintenance 互切并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateNode).mockResolvedValue({ id: 'n1' } as Awaited<ReturnType<typeof updateNode>>)

    const next = await store.toggleMaintenance({ id: 'n1', status: 'online' } as Parameters<typeof store.toggleMaintenance>[0])
    expect(next).toBe('maintenance')
    expect(updateNode).toHaveBeenCalledWith('n1', { status: 'maintenance' })

    await store.toggleMaintenance({ id: 'n1', status: 'maintenance' } as Parameters<typeof store.toggleMaintenance>[0])
    expect(updateNode).toHaveBeenLastCalledWith('n1', { status: 'online' })
    expect(listener).toHaveBeenCalledTimes(2)
    unsubscribe()
  })

  it('remove/getDetail 透传,remove 后 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(deleteNode).mockResolvedValue(undefined)
    await store.remove('n1')
    expect(deleteNode).toHaveBeenCalledWith('n1')
    expect(listener).toHaveBeenCalledTimes(1)

    vi.mocked(getNode).mockResolvedValue({ id: 'n1' } as Awaited<ReturnType<typeof getNode>>)
    expect(await store.getDetail('n1')).toMatchObject({ id: 'n1' })
    unsubscribe()
  })

  it('gpuSummary:空列表回退 —,同型号取首卡聚合', () => {
    expect(gpuSummary([])).toEqual({ label: '—', vram: '—', driver: '—' })
    const summary = gpuSummary([
      { name: 'A100', total_vram_gb: 80, driver_version: '570' },
      { name: 'A100', total_vram_gb: 80, driver_version: '570' },
    ] as Parameters<typeof gpuSummary>[0])
    expect(summary).toEqual({ label: 'A100 × 2', vram: '80GB', driver: '570' })

    const noDriver = gpuSummary([{ name: 'L4', total_vram_gb: 24, driver_version: null }] as Parameters<typeof gpuSummary>[0])
    expect(noDriver.driver).toBe('未安装')
  })

  it('formatPercent/formatGbPair:百分比取整、GB 用量对与空值回退', () => {
    expect(formatPercent(12.3)).toBe('12%')
    expect(formatPercent(62.5)).toBe('63%')
    expect(formatGbPair(45.6, 64)).toBe('45.6/64GB')
    expect(formatGbPair(120, 500)).toBe('120/500GB')
    expect(formatGbPair(10, null)).toBe('10/—GB')
  })

  it('progressColor:负载分级变色', () => {
    expect(progressColor(0)).toBe('bg-success')
    expect(progressColor(80)).toBe('bg-success')
    expect(progressColor(81)).toBe('bg-warning')
    expect(progressColor(90)).toBe('bg-warning')
    expect(progressColor(91)).toBe('bg-error')
    expect(progressColor(100)).toBe('bg-error')
  })

  it('formatDateTime 三分支,状态映射齐全', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('bad')).toBe('bad')
    expect(formatDateTime(new Date(2025, 0, 2, 3, 4).toISOString())).toBe('2025-01-02 03:04')
    expect(Object.keys(nodeStatusMap).sort()).toEqual(['busy', 'drained', 'maintenance', 'offline', 'online'])
    expect(gpuStatusMap.stale).toEqual({ label: '心跳过期', type: 'warning' })
  })
})
