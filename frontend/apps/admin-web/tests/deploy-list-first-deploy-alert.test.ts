// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  // route 必须响应式:useEntryAction 的 watch 依赖 route.query.action 的深度追踪
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
  startDeployment: vi.fn(),
  stopDeployment: vi.fn(),
}))
vi.mock('@/api/tail-log', () => ({ tailContainerLog: vi.fn() }))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useConfirm: vi.fn(),
    message: { success: vi.fn(), error: vi.fn() },
  }
})

import { listDeployments } from '@/api/deployment'
import type { ModelDeploymentOut } from '@/api/deployment'
import DeployList from '@/views/deploy/components/deploy-list/DeployList.vue'

/** 列表接口分页返回结构 */
type DeploymentPage = Awaited<ReturnType<typeof listDeployments>>

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

function emptyPage(): DeploymentPage {
  return { items: [], total: 0, page: 1, page_size: 10, total_pages: 1 }
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
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('DeployList 首次部署引导 alert', () => {
  it('表格有数据时不展示', async () => {
    const wrapper = mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('dep'))

    expect(document.body.textContent).not.toContain('首次部署建议')
    wrapper.unmount()
  })

  it('表格返回空数据时展示(与空态引导配套)', async () => {
    vi.mocked(listDeployments).mockResolvedValue(emptyPage())
    const wrapper = mount(DeployList, { attachTo: document.body })

    await vi.waitFor(() => expect(document.body.textContent).toContain('首次部署建议'))
    // 空态插槽同时给出直接新建入口
    expect(document.body.textContent).toContain('还没有部署')
    wrapper.unmount()
  })

  it('请求返回前不展示,返回后才按数据判断(默认不闪现)', async () => {
    let resolveList: ((value: DeploymentPage) => void) | undefined
    vi.mocked(listDeployments).mockImplementation(
      () => new Promise<DeploymentPage>((resolve) => (resolveList = resolve)),
    )
    const wrapper = mount(DeployList, { attachTo: document.body })
    await nextTick()
    await nextTick()

    // 请求未返回:保持默认隐藏
    expect(document.body.textContent).not.toContain('首次部署建议')

    resolveList?.(emptyPage())
    await vi.waitFor(() => expect(document.body.textContent).toContain('首次部署建议'))
    wrapper.unmount()
  })

  it('筛选后无结果时不展示(有生效查询条件不算首次使用)', async () => {
    vi.mocked(listDeployments).mockResolvedValue(emptyPage())
    const wrapper = mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('首次部署建议'))

    ;(wrapper.vm as unknown as { handleStatusChange: (value: string) => void }).handleStatusChange(
      'running',
    )
    await vi.waitFor(() => expect(listDeployments).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(document.body.textContent).not.toContain('首次部署建议'))
    wrapper.unmount()
  })
})
