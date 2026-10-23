import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/cluster', () => ({
  listClusters: vi.fn(),
  createCluster: vi.fn(),
  updateCluster: vi.fn(),
  deleteCluster: vi.fn(),
}))
vi.mock('@/api/node', () => ({ listNodes: vi.fn() }))
vi.mock('@/api/gpu', () => ({ listGpus: vi.fn() }))
vi.mock('@/api/quick-setup', () => ({
  markClusterCreated: vi.fn(),
  resetClusterCache: vi.fn(),
}))

import { createCluster, deleteCluster, listClusters, updateCluster, type ClusterData } from '@/api/cluster'
import { listNodes } from '@/api/node'
import { listGpus } from '@/api/gpu'
import { markClusterCreated, resetClusterCache } from '@/api/quick-setup'
import {
  CLUSTER_TYPE_OPTIONS,
  clusterStatusMap,
  useStore,
} from '@/views/node-pool/useStore'

function makeCluster(overrides: Partial<ClusterData> = {}): ClusterData {
  return {
    id: 1,
    name: 'gpu-shared',
    type: 'docker',
    status: 'active',
    ...overrides,
  } as ClusterData
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listClusters).mockResolvedValue({
    items: [makeCluster({ id: 1 }), makeCluster({ id: 2, name: 'cpu-only' })],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listClusters>>)
  vi.mocked(listNodes).mockResolvedValue({
    items: [
      { id: 'n1', cluster_id: 1, hostname: 'gpu-01' },
      { id: 'n2', cluster_id: 1, hostname: 'gpu-02' },
      { id: 'n3', cluster_id: 2, hostname: 'cpu-01' },
    ],
    total: 3,
    page: 1,
    page_size: 100,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listNodes>>)
  vi.mocked(listGpus).mockResolvedValue({
    items: [
      { id: 1, uuid: 'g1', node_id: 'n1', index: 0, name: 'A100' },
      { id: 2, uuid: 'g2', node_id: 'n1', index: 1, name: 'A100' },
      { id: 3, uuid: 'g3', node_id: 'n-unknown', index: 0, name: 'A100' },
    ],
    total: 3,
    page: 1,
    page_size: 100,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listGpus>>)
})

describe('node-pool useStore', () => {
  it('request 聚合集群+节点数+GPU 总量(经 node→cluster 映射,游离 GPU 不计)', async () => {
    const result = await store.request({ page: 1, pageSize: 20 })
    const shared = result.data.find((row) => row.id === 1)!
    expect(shared).toMatchObject({ nodeCount: 2, gpuTotal: 2 }) // n-unknown 的 GPU 不计入
    const cpuOnly = result.data.find((row) => row.id === 2)!
    expect(cpuOnly).toMatchObject({ nodeCount: 1, gpuTotal: 0 })
    expect(listClusters).toHaveBeenCalledWith({ page: 1, page_size: 20 }, undefined)
  })

  it('save 无 id 创建:同步向导守卫缓存 markClusterCreated;有 id 更新不调用', async () => {
    vi.mocked(createCluster).mockResolvedValue(makeCluster({ id: 9 }))
    const created = await store.save({ name: '新集群', type: 'k8s', description: '' })
    expect(created.mode).toBe('create')
    expect(createCluster).toHaveBeenCalledWith({ name: '新集群', type: 'k8s', description: '' })
    expect(markClusterCreated).toHaveBeenCalledTimes(1)

    vi.mocked(updateCluster).mockResolvedValue(makeCluster())
    const updated = await store.save({ id: 1, name: '改名', type: 'docker', description: 'd' })
    expect(updated.mode).toBe('update')
    expect(updateCluster).toHaveBeenCalledWith(1, { name: '改名', description: 'd' })
    expect(markClusterCreated).toHaveBeenCalledTimes(1) // 更新不再调用
  })

  it('toggleStatus:active↔inactive 互切并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateCluster).mockResolvedValue(makeCluster())

    const next = await store.toggleStatus(makeCluster({ id: 1, status: 'active' }) as Parameters<typeof store.toggleStatus>[0])
    expect(next).toBe('inactive')
    expect(updateCluster).toHaveBeenCalledWith(1, { status: 'inactive' })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('removePool 删除后 resetClusterCache 并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(deleteCluster).mockResolvedValue(undefined)

    await store.removePool(1)
    expect(deleteCluster).toHaveBeenCalledWith(1)
    expect(resetClusterCache).toHaveBeenCalledTimes(1)
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('状态/选项常量', () => {
    expect(clusterStatusMap.inactive).toEqual({ label: '停用', type: 'default' })
    expect(CLUSTER_TYPE_OPTIONS.map((item) => item.value)).toEqual(['docker', 'k8s'])
  })
})
