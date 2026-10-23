// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const route = reactive({ query: {} as Record<string, unknown> })
  const router = { push: vi.fn(), replace: vi.fn() }
  return {
    useRoute: () => route,
    useRouter: () => router,
    __route: route,
    __router: router,
  }
})
// VueFlow 依赖画布测量/ResizeObserver,测试环境以插槽桩替代
vi.mock('@vue-flow/core', async () => {
  const { ref } = await import('vue')
  return {
    VueFlow: {
      name: 'VueFlow',
      props: ['nodes', 'edges'],
      template:
        '<div><template v-for="n in nodes" :key="n.id"><slot :name="`node-${n.type}`" v-bind="n" /></template><slot /></div>',
    },
    Handle: { name: 'Handle', template: '<div />' },
    Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
    useVueFlow: () => ({
      fitView: vi.fn(),
      onNodeClick: vi.fn(),
      onPaneClick: vi.fn(),
    }),
    useNodesInitialized: () => ref(true),
  }
})
vi.mock('@vue-flow/background', () => ({ Background: { template: '<div />' } }))
vi.mock('@vue-flow/controls', () => ({ Controls: { template: '<div />' } }))
vi.mock('@vue-flow/minimap', () => ({ MiniMap: { template: '<div />' } }))
vi.mock('@/api/deployment', async (importOriginal) => ({
  // normalizedTopology 是纯函数,直接用真实实现(拓扑双写归一取数)
  ...(await importOriginal<typeof import('@/api/deployment')>()),
  createDeployment: vi.fn(),
  deleteDeployment: vi.fn(),
  getDeployment: vi.fn(),
  getDeploymentNodeSummary: vi.fn(),
  listDeploymentLogs: vi.fn(),
  listDeployments: vi.fn(),
  scaleDeployment: vi.fn(),
  scalePreview: vi.fn(),
  startDeployment: vi.fn(),
  stopDeployment: vi.fn(),
}))
vi.mock('@/api/topology', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/topology')>()),
  getClusterTopology: vi.fn(),
}))
vi.mock('@/api/tail-log', () => ({ tailContainerLog: vi.fn() }))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useConfirm: vi.fn(),
    // 扩缩弹窗走全局 useDialog:mock 后断言入参,不真渲染弹窗(渲染细节由 scale-dialog.test.ts 覆盖)
    useDialog: vi.fn(),
    message: { success: vi.fn(), error: vi.fn() },
  }
})

import { getClusterTopology } from '@/api/topology'
import { getDeployment, listDeployments } from '@/api/deployment'
import type { ClusterTopology, ModelDeploymentOut } from '@/api/deployment'
import { useDialog } from '@repo/hooks'
import DeployList from '@/views/deploy/components/deploy-list/DeployList.vue'
import ScaleDialog from '@/views/deploy/components/deploy-list/deploy-detail/components/ScaleDialog.vue'

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
    spec: { topology: { tp: 2, pp: 1, replicas: 1, nnodes: 2, max_model_len: 8192 } },
    created_at: '2025-01-01T08:30:00Z',
    updated_at: '2025-01-01T08:30:00Z',
    ...overrides,
  } as ModelDeploymentOut
}

function mockTopology(): void {
  vi.mocked(getClusterTopology).mockResolvedValue({
    nodes: [{ id: 'n1', hostname: 'gpu-01' }],
    gpus: [],
  } as unknown as ClusterTopology)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listDeployments).mockResolvedValue({
    items: [makeRow()],
    total: 1,
    page: 1,
    page_size: 10,
    total_pages: 1,
  })
  mockTopology()
})

afterEach(() => {
  document.body.innerHTML = ''
})

/** 表头文字清单 */
const headerTexts = (): string[] =>
  [...document.body.querySelectorAll('thead th')].map((th) => th.textContent ?? '')

/** 行内指定下标的单元格(0 选择列 / 1 部署名 / … / 6 副本数) */
function cellOf(row: Element, index: number): HTMLTableCellElement {
  return row.querySelectorAll('td')[index] as HTMLTableCellElement
}

describe('DeployList 列表列(算力 / 副本数 / 端点移除)', () => {
  it('算力列展示单副本规格「N 节点 · M 卡」,端点列已移除', async () => {
    mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('dep'))

    const headers = headerTexts()
    expect(headers).toContain('算力')
    expect(headers).toContain('副本数')
    expect(headers).not.toContain('端点')

    // tp=2 nnodes=2 replicas=1 → 单副本 2 节点 · 2 卡;副本数列展示 1 份
    expect(document.body.textContent).toContain('2 节点 · 2 卡')
    expect(document.body.textContent).toContain('1 份')
  })

  it('running 行副本数可点击:拉详情与集群拓扑后打开扩缩弹窗,透传副本序号与 hostname 映射', async () => {
    vi.mocked(getDeployment).mockResolvedValue({
      deployment: makeRow(),
      instances: [],
      endpoints: [],
      replicas: [
        // 后端 seq 为 0 基;透传给扩缩弹窗的 replicaSeqs 统一转 1 基人读序号
        { replica_id: 'r1', seq: 0, status: 'serving', engines: [], endpoint_ids: [] },
        { replica_id: 'r2', seq: 1, status: 'serving', engines: [], endpoint_ids: [] },
      ],
    })
    const wrapper = mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('dep'))

    const copiesCell = cellOf(
      [...document.body.querySelectorAll('tbody tr')].find((row) =>
        row.textContent?.includes('dep'),
      )!,
      6,
    )
    copiesCell.querySelector('span')!.click()

    await vi.waitFor(() => expect(useDialog).toHaveBeenCalledTimes(1))
    expect(getDeployment).toHaveBeenCalledWith('d1')
    expect(getClusterTopology).toHaveBeenCalledWith(1)

    const dialogArg = vi.mocked(useDialog).mock.calls[0]![0]
    expect(dialogArg.title).toBe('调整副本 · dep')
    expect(dialogArg.component).toBe(ScaleDialog)
    expect(dialogArg.props).toMatchObject({
      deploymentId: 'd1',
      deploymentName: 'dep',
      currentCopies: 1,
      replicaSeqs: [1, 2],
      hostnameOf: { n1: 'gpu-01' },
    })
    expect(typeof dialogArg.props.onScaled).toBe('function')
    wrapper.unmount()
  })

  it('非 running 行副本数纯展示不可点击;SCALING 子态展示「扩缩容中」徽标', async () => {
    vi.mocked(listDeployments).mockResolvedValue({
      items: [
        makeRow({ deployment_id: 'd-stop', name: 'dep-stop', status: 'stopped' }),
        makeRow({ deployment_id: 'd-scale', name: 'dep-scaling', status: 'running', phase: 'SCALING' }),
      ],
      total: 2,
      page: 1,
      page_size: 10,
      total_pages: 1,
    })
    const wrapper = mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('dep-stop'))

    const rows = [...document.body.querySelectorAll('tbody tr')]
    const stopCell = cellOf(
      rows.find((row) => row.textContent?.includes('dep-stop'))!,
      6,
    )
    // stopped:纯文本(无 span 包裹),点击单元格不触发任何请求
    expect(stopCell.textContent).toContain('1 份')
    expect(stopCell.querySelector('.text-primary')).toBeNull()
    stopCell.click()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(getDeployment).not.toHaveBeenCalled()

    // SCALING 子态:徽标替代数字,无点击入口(后端 409 防重入)
    const scaleCell = cellOf(
      rows.find((row) => row.textContent?.includes('dep-scaling'))!,
      6,
    )
    expect(scaleCell.textContent).toContain('扩缩容中')
    expect(scaleCell.querySelector('.text-primary')).toBeNull()
    wrapper.unmount()
  })
})
