import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/deployment', async (importOriginal) => ({
  // normalizedTopology 是纯函数,直接用真实实现(两种拓扑写法归一取数)
  ...(await importOriginal<typeof import('@/api/deployment')>()),
  createDeployment: vi.fn(),
  deleteDeployment: vi.fn(),
  getDeployment: vi.fn(),
  listDeployments: vi.fn(),
  startDeployment: vi.fn(),
  stopDeployment: vi.fn(),
}))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))

import {
  createDeployment,
  deleteDeployment,
  getDeployment,
  listDeployments,
  startDeployment,
  stopDeployment,
} from '@/api/deployment'
import { listModels } from '@/api/model'
import type { ModelDeploymentOut } from '@/api/deployment'
import {
  DEPLOY_CREATE_PHASES,
  DEPLOY_PHASE_LABEL,
  DEPLOY_PHASE_STEPS,
  DEPLOY_STATUS_OPTIONS,
  deployStatusMap,
  formatDateTime,
  gpuSummary,
  useStore,
} from '@/views/deploy/components/deploy-list/useStore'

function makeRow(overrides: Partial<ModelDeploymentOut> = {}): ModelDeploymentOut {
  return {
    deployment_id: 'd1',
    cluster_id: 1,
    name: 'dep',
    model_id: 1,
    engine: 'vllm',
    status: 'running',
    api_endpoint: null,
    last_error: null,
    phase: 'RUNNING',
    version: 1,
    spec: {},
    created_at: '2025-01-01T08:30:00Z',
    updated_at: '2025-01-01T08:30:00Z',
    ...overrides,
  } as ModelDeploymentOut
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
})

// ---------- 纯函数 ----------

describe('gpuSummary 算力概要(单副本规格)', () => {
  it('无拓扑信息时显示 --', () => {
    expect(gpuSummary(makeRow())).toBe('--')
    expect(gpuSummary(makeRow({ spec: { topology: {} } }))).toBe('--')
  })

  it('单节点:tp=4 replicas=1 → 1 节点 · 4 卡', () => {
    const row = makeRow({ spec: { topology: { tp: 4, replicas: 1, nnodes: 1 } } })
    expect(gpuSummary(row)).toBe('1 节点 · 4 卡')
  })

  it('多副本只给单副本口径(总量 = 卡数 × 副本数):tp=2 replicas=3 → 1 节点 · 2 卡', () => {
    const row = makeRow({ spec: { topology: { tp: 2, replicas: 3, nnodes: 1 } } })
    expect(gpuSummary(row)).toBe('1 节点 · 2 卡')
  })

  it('跨机:tp=4 nnodes=2 → 2 节点 · 4 卡', () => {
    const row = makeRow({ spec: { topology: { tp: 4, replicas: 1, nnodes: 2 } } })
    expect(gpuSummary(row)).toBe('2 节点 · 4 卡')
  })

  it('units/copies 权威写法:取首单元 nnodes 与卡数', () => {
    const row = makeRow({
      spec: { topology: { units: [{ role: 'full', tp: 2, pp: 1, ep: 1, nnodes: 2 }], copies: 3 } },
    })
    expect(gpuSummary(row)).toBe('2 节点 · 2 卡')
  })
})

describe('formatDateTime 时间格式化', () => {
  it('空值显示 —', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('')).toBe('—')
  })

  it('非法时间字符串原样返回', () => {
    expect(formatDateTime('not-a-date')).toBe('not-a-date')
  })

  it('ISO8601 格式化为 YYYY-MM-DD HH:mm', () => {
    const formatted = formatDateTime('2025-06-01T09:05:00')
    expect(formatted).toBe('2025-06-01 09:05')
  })

  it('月/日/时/分不足两位补零', () => {
    const formatted = formatDateTime(new Date(2025, 0, 2, 3, 4).toISOString())
    expect(formatted).toBe('2025-01-02 03:04')
  })
})

describe('状态映射常量', () => {
  it('deployStatusMap 覆盖全部五个状态', () => {
    expect(Object.keys(deployStatusMap).sort()).toEqual([
      'creating',
      'deleting',
      'failed',
      'running',
      'stopped',
    ])
    expect(deployStatusMap.running).toEqual({ label: '运行中', type: 'success' })
    expect(deployStatusMap.failed.type).toBe('error')
  })

  it('DEPLOY_STATUS_OPTIONS 首项为全部状态(空值)', () => {
    expect(DEPLOY_STATUS_OPTIONS[0]).toEqual({ label: '全部状态', value: '' })
  })

  it('DEPLOY_PHASE_LABEL 覆盖编排全子态', () => {
    expect(DEPLOY_PHASE_LABEL.PENDING).toBe('排队中')
    expect(DEPLOY_PHASE_LABEL.STAGING_MODEL).toBe('下载模型')
    expect(DEPLOY_PHASE_LABEL.RUNNING).toBe('就绪')
    expect(DEPLOY_PHASE_LABEL.SCALING).toBe('扩缩容中')
    expect(Object.keys(DEPLOY_PHASE_LABEL)).toHaveLength(11)
  })

  it('DEPLOY_CREATE_PHASES 为有序创建流水线(不含拆除流程 STOPPING),步骤条选项文案对齐', () => {
    expect(DEPLOY_CREATE_PHASES).toEqual([
      'PENDING',
      'STAGING_MODEL',
      'PULL_IMAGE',
      'START_HEAD',
      'WAIT_HEAD_READY',
      'START_WORKERS',
      'WAIT_WORKERS_READY',
      'FINALIZING',
      'RUNNING',
    ])
    expect(DEPLOY_CREATE_PHASES).not.toContain('STOPPING')
    expect(DEPLOY_PHASE_STEPS).toHaveLength(DEPLOY_CREATE_PHASES.length)
    expect(DEPLOY_PHASE_STEPS[0]).toEqual({ key: 'PENDING', label: '排队中' })
    expect(DEPLOY_PHASE_STEPS[DEPLOY_PHASE_STEPS.length - 1]).toEqual({
      key: 'RUNNING',
      label: '就绪',
    })
  })
})

// ---------- 数据层 ----------

describe('request 分页请求', () => {
  it('透传 page/page_size 与状态筛选,返回表格契约 { data, total }', async () => {
    vi.mocked(listDeployments).mockResolvedValue({
      items: [makeRow()],
      total: 1,
      page: 1,
      page_size: 20,
      total_pages: 1,
    })

    const result = await store.request({ page: 1, pageSize: 20, status: 'running' })

    expect(listDeployments).toHaveBeenCalledWith(
      { page: 1, page_size: 20, status: 'running' },
      undefined,
    )
    expect(result).toEqual({ data: [expect.objectContaining({ deployment_id: 'd1' })], total: 1 })
  })

  it('状态为空串时不传 status(全部)', async () => {
    vi.mocked(listDeployments).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
      total_pages: 0,
    })

    await store.request({ page: 2, pageSize: 20, status: '' })

    expect(listDeployments).toHaveBeenCalledWith({ page: 2, page_size: 20 }, undefined)
  })

  it('透传 useTable 的取消信号', async () => {
    vi.mocked(listDeployments).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
      total_pages: 0,
    })
    const controller = new AbortController()

    await store.request({ page: 1, pageSize: 20 }, { signal: controller.signal })

    expect(listDeployments).toHaveBeenCalledWith(expect.anything(), controller.signal)
  })
})

describe('modelName 模型名回显', () => {
  it('懒加载一次并缓存,未命中回退 #id', async () => {
    vi.mocked(listModels).mockResolvedValue({
      items: [
        { id: 1, name: 'Qwen2.5-7B' },
        { id: 2, name: 'Llama3-8B' },
      ],
    } as Awaited<ReturnType<typeof listModels>>)

    expect(store.modelName(1)).toBe('#1')

    await store.loadModelNames()
    await store.loadModelNames() // 幂等:只请求一次

    expect(listModels).toHaveBeenCalledTimes(1)
    expect(store.modelName(1)).toBe('Qwen2.5-7B')
    expect(store.modelName(99)).toBe('#99')
  })

  it('强制重拉失败静默不抛异常,且保留上次成功加载的映射', async () => {
    vi.mocked(listModels).mockRejectedValue(new Error('boom'))
    await expect(store.loadModelNames(true)).resolves.toBeUndefined()
    // 失败不清空旧映射:已加载的模型名仍可回显
    expect(store.modelName(1)).toBe('Qwen2.5-7B')
  })

  it('首次加载即失败时回退 #id 展示', async () => {
    // 模块级单例状态已被其它用例污染,隔离模块重验证初始失败路径
    vi.resetModules()
    vi.doMock('@/api/model', () => ({ listModels: vi.fn().mockRejectedValue(new Error('boom')) }))
    const fresh = await import('@/views/deploy/components/deploy-list/useStore')
    await expect(fresh.useStore().loadModelNames()).resolves.toBeUndefined()
    expect(fresh.useStore().modelName(1)).toBe('#1')
  })
})

describe('写操作 notify 联动', () => {
  it('createDeploy 透传 payload,成功后 notify 联动订阅方', async () => {
    vi.mocked(createDeployment).mockResolvedValue(makeRow())
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    const result = await store.createDeploy({
      cluster_id: 1,
      name: 'dep',
      model_id: 7,
    })

    expect(createDeployment).toHaveBeenCalledTimes(1)
    expect(result.deployment_id).toBe('d1')
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('stop/remove 成功后 notify,失败时不 notify 且异常上抛', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    vi.mocked(stopDeployment).mockResolvedValue({ deployment_id: 'd1', status: 'stopped' })
    await store.stop('d1')
    expect(listener).toHaveBeenCalledTimes(1)

    vi.mocked(deleteDeployment).mockRejectedValueOnce(new Error('boom'))
    await expect(store.remove('d1')).rejects.toThrow('boom')
    expect(listener).toHaveBeenCalledTimes(1) // 失败不触发

    vi.mocked(deleteDeployment).mockResolvedValue({ deployment_id: 'd2', deleted: true })
    await store.remove('d2')
    expect(listener).toHaveBeenCalledTimes(2)

    unsubscribe()
  })

  it('start 透传 force 参数,成功后 notify;失败时不 notify 且异常上抛', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    vi.mocked(startDeployment).mockResolvedValue({})
    await store.start('d1')
    expect(startDeployment).toHaveBeenCalledWith('d1', false)
    expect(listener).toHaveBeenCalledTimes(1)

    // 409 占用确认后强制启动:force=true 透传
    await store.start('d1', true)
    expect(startDeployment).toHaveBeenCalledWith('d1', true)
    expect(listener).toHaveBeenCalledTimes(2)

    vi.mocked(startDeployment).mockRejectedValueOnce(new Error('boom'))
    await expect(store.start('d1')).rejects.toThrow('boom')
    expect(listener).toHaveBeenCalledTimes(2) // 失败不触发

    unsubscribe()
  })

  it('removeMany 批量删除:全部完成后统一 notify 一次', async () => {
    vi.mocked(deleteDeployment).mockResolvedValue({ deployment_id: 'x', deleted: true })
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    await store.removeMany(['d1', 'd2', 'd3'])

    expect(deleteDeployment).toHaveBeenCalledTimes(3)
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('subscribe 返回的取消函数生效,退订后不再收到通知', async () => {
    vi.mocked(deleteDeployment).mockResolvedValue({ deployment_id: 'd1', deleted: true })
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    unsubscribe()
    await store.remove('d1')

    expect(listener).not.toHaveBeenCalled()
  })
})

describe('fetchDetail 详情透传', () => {
  it('按 deploymentId 请求详情', async () => {
    vi.mocked(getDeployment).mockResolvedValue({
      deployment: makeRow(),
      instances: [],
      endpoints: [],
    })

    await store.fetchDetail('d1')

    expect(getDeployment).toHaveBeenCalledWith('d1')
  })
})
