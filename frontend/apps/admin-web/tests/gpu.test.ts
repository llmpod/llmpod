import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/gpu', () => ({ listGpus: vi.fn() }))
vi.mock('@/api/node', () => ({ listNodes: vi.fn() }))

import { listGpus } from '@/api/gpu'
import { listNodes } from '@/api/node'
import {
  formatGbPair,
  formatPercent,
  gpuStatusMap,
  progressColor,
  useStore,
} from '@/views/gpu/useStore'

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listGpus).mockResolvedValue({
    items: [
      {
        id: 1,
        uuid: 'g1',
        node_id: 'n1',
        hostname: '',
        name: 'A100',
        status: 'online',
        runtime: {
          used_vram_gb: 0.52,
          memory_utilization: 2.3,
          utilization: 35.6,
          temperature: 44,
          fan_speed: 27,
          power_draw_w: 20.8,
          power_limit_w: 250.0,
          sm_clock_mhz: 300,
          mem_clock_mhz: 405,
          process_count: 0,
        },
      },
      { id: 2, uuid: 'g2', node_id: 'n-unknown', hostname: '', name: 'A100', status: 'stale', runtime: null },
    ],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listGpus>>)
  vi.mocked(listNodes).mockResolvedValue({
    items: [{ id: 'n1', hostname: 'gpu-node-01' }],
    total: 1,
    page: 1,
    page_size: 100,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listNodes>>)
})

describe('gpu useStore', () => {
  it('request 聚合 GPU 与节点主机名,未知节点回退 node_id', async () => {
    const result = await store.request({ page: 1, pageSize: 20 })
    expect(result.data[0].nodeHost).toBe('gpu-node-01')
    expect(result.data[1].nodeHost).toBe('n-unknown') // 未命中回退原始 id
    expect(result.total).toBe(2)
  })

  it('runtime 随列表透传,离线卡保持 null 供展开行判空', async () => {
    const result = await store.request({ page: 1, pageSize: 20 })
    expect(result.data[0].runtime?.utilization).toBe(35.6)
    expect(result.data[1].runtime).toBeNull()
  })

  it('page_size 字段兼容:运行时 page_size 优先于 pageSize', async () => {
    await store.request({ page: 2, pageSize: 20, page_size: 50 } as { page: number; pageSize: number })
    expect(listGpus).toHaveBeenCalledWith({ page: 2, page_size: 50 }, undefined)
    // 节点主机名映射一次拉全量(上限 100)
    expect(listNodes).toHaveBeenCalledWith({ page: 1, page_size: 100 }, undefined)
  })

  it('AbortSignal 透传给两个接口', async () => {
    const controller = new AbortController()
    await store.request({ page: 1, pageSize: 20 }, { signal: controller.signal })
    expect(listGpus).toHaveBeenCalledWith(expect.anything(), controller.signal)
    expect(listNodes).toHaveBeenCalledWith(expect.anything(), controller.signal)
  })

  it('状态映射覆盖三种状态', () => {
    expect(gpuStatusMap.online).toEqual({ label: '在线', type: 'success' })
    expect(gpuStatusMap.stale).toEqual({ label: '心跳过期', type: 'warning' })
    expect(gpuStatusMap.offline).toEqual({ label: '离线', type: 'error' })
  })

  it('formatPercent 四舍五入取整加 %', () => {
    expect(formatPercent(35.6)).toBe('36%')
    expect(formatPercent(0)).toBe('0%')
    expect(formatPercent(2.3)).toBe('2%')
  })

  it('formatGbPair 输出 used/totalGB 文案', () => {
    expect(formatGbPair(0.52, 22)).toBe('0.52/22GB')
  })

  it('progressColor 按阈值分色:<=80 绿,>80 黄,>90 红', () => {
    expect(progressColor(80)).toBe('bg-success')
    expect(progressColor(81)).toBe('bg-warning')
    expect(progressColor(91)).toBe('bg-error')
  })
})
