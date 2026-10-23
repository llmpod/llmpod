// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/cluster', () => ({
  listClusters: vi.fn(),
  createCluster: vi.fn(),
}))
vi.mock('@/api/node', () => ({ listNodes: vi.fn() }))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@/api/deployment', () => ({ getDeployment: vi.fn() }))
vi.mock('@/api/quick-setup', () => ({
  isSetupMock: () => false,
  markClusterCreated: vi.fn(),
  QUICK_SETUP_DONE_KEY: 'llmpod:quick-setup:done',
  uploadModel: vi.fn(),
}))

import { createCluster, listClusters, type ClusterData } from '@/api/cluster'
import { getDeployment } from '@/api/deployment'
import { listModels } from '@/api/model'
import { listNodes } from '@/api/node'
import { useStore } from '@/views/quick-setup/useStore'

const store = useStore()

function makeCluster(overrides: Partial<ClusterData> = {}): ClusterData {
  return { id: 1, name: '默认集群', type: 'docker', status: 'active', ...overrides } as ClusterData
}

beforeEach(async () => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  store.reset()
  vi.mocked(listClusters).mockResolvedValue({
    items: [makeCluster()],
    total: 1,
    page: 1,
    page_size: 1,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listClusters>>)
  vi.mocked(listNodes).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 100,
    total_pages: 0,
  } as Awaited<ReturnType<typeof listNodes>>)
  vi.mocked(listModels).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 1,
    total_pages: 0,
  } as Awaited<ReturnType<typeof listModels>>)
  await store.init()
  vi.mocked(listNodes).mockClear()
})

afterEach(() => {
  store.stopPolling()
  vi.useRealTimers()
})

describe('quick-setup store 轮询按步骤收敛', () => {
  it('步骤1(添加节点):轮询同步节点列表', async () => {
    store.startPolling()
    await vi.advanceTimersByTimeAsync(4000)
    expect(listNodes).toHaveBeenCalled()
  })

  it('进入步骤2 后:不再轮询节点列表', async () => {
    store.startPolling()
    store.goToStep(2)
    await vi.advanceTimersByTimeAsync(10000)
    expect(listNodes).not.toHaveBeenCalled()
  })

  it('进入后续步骤:部署中任务的状态轮询不受影响', async () => {
    vi.mocked(getDeployment).mockResolvedValue({
      deployment: { status: 'running' },
    } as Awaited<ReturnType<typeof getDeployment>>)
    store.applyDeploy({ deployment_id: 'd1', name: 'svc', status: 'creating' } as never)
    store.startPolling()
    store.goToStep(3)
    await vi.advanceTimersByTimeAsync(4000)
    expect(getDeployment).toHaveBeenCalledWith('d1')
    expect(store.deploy.value?.status).toBe('running')
  })
})

describe('quick-setup store 多集群选择', () => {
  it('init 拉取全量集群,默认选中第一个', async () => {
    const first = makeCluster({ id: 1, name: 'cluster-a' })
    const second = makeCluster({ id: 2, name: 'cluster-b' })
    vi.mocked(listClusters).mockResolvedValue({
      items: [first, second],
      total: 2,
      page: 1,
      page_size: 100,
      total_pages: 1,
    } as Awaited<ReturnType<typeof listClusters>>)
    vi.mocked(listNodes).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 100,
      total_pages: 0,
    } as Awaited<ReturnType<typeof listNodes>>)
    await store.init()
    expect(listClusters).toHaveBeenCalledWith({ page: 1, page_size: 100 })
    expect(store.clusters.value).toHaveLength(2)
    expect(store.cluster.value?.id).toBe(1)
    // 默认选中集群的节点列表被拉取
    expect(listNodes).toHaveBeenCalledWith({ cluster_id: 1, page: 1, page_size: 100 })
  })

  it('switchCluster 切换上下文:按新集群拉节点,部署状态清空', async () => {
    const first = makeCluster({ id: 1, name: 'cluster-a' })
    const second = makeCluster({ id: 2, name: 'cluster-b' })
    vi.mocked(listClusters).mockResolvedValue({
      items: [first, second],
      total: 2,
      page: 1,
      page_size: 100,
      total_pages: 1,
    } as Awaited<ReturnType<typeof listClusters>>)
    await store.init()
    store.applyDeploy({ deployment_id: 'd1', name: 'svc', status: 'creating' } as never)
    expect(store.deploy.value).not.toBeNull()

    vi.mocked(listNodes).mockResolvedValue({
      items: [
        { id: 'n2', hostname: 'node-b', ip: '192.168.1.20', status: 'online' } as never,
      ],
      total: 1,
      page: 1,
      page_size: 100,
      total_pages: 1,
    } as Awaited<ReturnType<typeof listNodes>>)
    await store.switchCluster(2)

    expect(store.cluster.value?.id).toBe(2)
    expect(store.deploy.value).toBeNull()
    expect(listNodes).toHaveBeenCalledWith({ cluster_id: 2, page: 1, page_size: 100 })
    expect(store.nodes.value).toHaveLength(1)
    expect(store.nodes.value[0]?.hostname).toBe('node-b')
  })

  it('switchCluster 传不存在/当前集群 id 时不动作', async () => {
    expect(store.cluster.value?.id).toBe(1)
    await store.switchCluster(999)
    expect(store.cluster.value?.id).toBe(1)
    await store.switchCluster(1)
    expect(store.cluster.value?.id).toBe(1)
    expect(listNodes).not.toHaveBeenCalled()
  })

  it('submitCluster 新建后入列并选中新集群', async () => {
    const created = makeCluster({ id: 5, name: 'new-cluster' })
    vi.mocked(createCluster).mockResolvedValue(created)
    await store.submitCluster({ name: 'new-cluster', type: 'docker', description: '' })
    expect(store.cluster.value?.id).toBe(5)
    expect(store.clusters.value.map((c) => c.id)).toContain(5)
    expect(store.nodes.value).toHaveLength(0)
  })
})
