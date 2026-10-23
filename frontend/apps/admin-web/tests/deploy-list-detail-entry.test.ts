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
// VueFlow 依赖画布测量/ResizeObserver,测试环境以插槽桩替代:按节点类型渲染对应卡片插槽
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

import { getDeployment, getDeploymentNodeSummary, listDeploymentLogs, listDeployments, startDeployment } from '@/api/deployment'
import type { ModelDeploymentOut } from '@/api/deployment'
import { useConfirm, message } from '@repo/hooks'
import DeployList from '@/views/deploy/components/deploy-list/DeployList.vue'
import * as vueRouterMock from 'vue-router'

const route = (vueRouterMock as unknown as { __route: { query: Record<string, unknown> } }).__route
const router = (vueRouterMock as unknown as { __router: { replace: ReturnType<typeof vi.fn> } }).__router

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

beforeEach(() => {
  vi.clearAllMocks()
  route.query = {}
  vi.mocked(listDeployments).mockResolvedValue({
    items: [makeRow()],
    total: 1,
    page: 1,
    page_size: 10,
    total_pages: 1,
  })
  // 日志区时间线默认空页
  vi.mocked(listDeploymentLogs).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 500,
    total_pages: 1,
  })
  vi.mocked(getDeployment).mockResolvedValue({
    deployment: makeRow(),
    instances: [],
    endpoints: [],
  })
  vi.mocked(getDeploymentNodeSummary).mockResolvedValue([])
})

afterEach(() => {
  route.query = {}
  document.body.innerHTML = ''
})

describe('DeployList 行操作「部署详情」', () => {
  it('点击后整个子 tab 切换为拓扑视图;退出回到表格', async () => {
    const wrapper = mount(DeployList, { attachTo: document.body })
    // 等表格渲染出行
    await vi.waitFor(() => expect(document.body.textContent).toContain('dep'))

    const btn = [...document.body.querySelectorAll('button')].find((item) =>
      item.textContent?.includes('部署详情'),
    )
    expect(btn).toBeTruthy()
    btn!.click()
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalledWith('d1'))
    await nextTick()

    // 拓扑视图:部署卡片 + 返回按钮;表格被覆盖
    expect(document.body.textContent).toContain('部署详情-dep')
    expect(document.body.textContent).not.toContain('还没有部署')

    // 返回:回到表格
    const backBtn = [...document.body.querySelectorAll('button')].find((item) =>
      item.textContent?.includes('返回'),
    )!
    backBtn.click()
    await nextTick()
    await nextTick()
    expect(document.body.textContent).not.toContain('部署详情-dep')
    wrapper.unmount()
  })
})

describe('DeployList 入口动作「?action=detail&id=」', () => {
  it('进入时按 id 拉取详情并直接打开拓扑视图,清掉 action/id 保留 tab', async () => {
    route.query = { action: 'detail', id: 'd1', tab: 'list' }
    const wrapper = mount(DeployList, { attachTo: document.body })

    await vi.waitFor(() => expect(document.body.textContent).toContain('部署详情-dep'))
    expect(getDeployment).toHaveBeenCalledWith('d1')
    expect(router.replace).toHaveBeenCalledWith({ query: { tab: 'list' } })
    wrapper.unmount()
  })

  it('id 缺失时不请求详情,停留在列表', async () => {
    route.query = { action: 'detail', tab: 'list' }
    const wrapper = mount(DeployList, { attachTo: document.body })

    await vi.waitFor(() => expect(document.body.textContent).toContain('dep'))
    expect(getDeployment).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toContain('部署详情-dep')
    wrapper.unmount()
  })

  it('详情接口在途时以骨架占位替代列表,避免先闪列表再切详情', async () => {
    // 手动控制详情接口时序:挂载后挂起,断言占位态后再放行
    let resolveDetail!: (value: unknown) => void
    vi.mocked(getDeployment).mockReturnValue(
      new Promise((resolve) => {
        resolveDetail = resolve
      }) as never,
    )
    route.query = { action: 'detail', id: 'd1' }
    const wrapper = mount(DeployList, { attachTo: document.body })

    // 详情在途:渲染骨架占位,列表工具条(新建部署)不出现
    await vi.waitFor(() => expect(document.querySelector('.n-skeleton')).toBeTruthy())
    expect(
      [...document.body.querySelectorAll('button')].some((btn) =>
        btn.textContent?.includes('新建部署'),
      ),
    ).toBe(false)

    // 详情返回:直接进入拓扑视图
    resolveDetail({ deployment: makeRow(), instances: [], endpoints: [] })
    await vi.waitFor(() => expect(document.body.textContent).toContain('部署详情-dep'))
    wrapper.unmount()
  })
})

describe('DeployList 入口动作「?action=status&status=」', () => {
  it('进入时按状态自动筛选,清掉 action/status', async () => {
    route.query = { action: 'status', status: 'running' }
    const wrapper = mount(DeployList, { attachTo: document.body })

    await vi.waitFor(() =>
      expect(listDeployments).toHaveBeenCalledWith(
        expect.objectContaining({ status: 'running' }),
        expect.anything(),
      ),
    )
    expect(router.replace).toHaveBeenCalledWith({ query: {} })
    wrapper.unmount()
  })

  it('初始进入仅发一次列表请求:status 入口合入首刷,不与 immediate 首刷重复', async () => {
    route.query = { action: 'status', status: 'running' }
    const wrapper = mount(DeployList, { attachTo: document.body })

    await vi.waitFor(() => expect(document.body.textContent).toContain('dep'))
    // 首刷即带上筛选:useEntryAction(immediate) 只写参数,表格 onMounted 首刷统一发出
    expect(listDeployments).toHaveBeenCalledTimes(1)
    expect(listDeployments).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'running' }),
      expect.anything(),
    )
    wrapper.unmount()
  })

  it('已停留在页面时收到 action=status 仍主动刷新(不依赖首刷)', async () => {
    const wrapper = mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(listDeployments).toHaveBeenCalledTimes(1))

    // 组件不重挂载,仅变更 query(全局搜索操作落地场景):watch 触发,search 主动请求
    route.query = { action: 'status', status: 'failed' }
    await vi.waitFor(() =>
      expect(listDeployments).toHaveBeenCalledWith(
        expect.objectContaining({ status: 'failed' }),
        expect.anything(),
      ),
    )
    expect(listDeployments).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('非法状态值不触发筛选,仅清理 query', async () => {
    route.query = { action: 'status', status: 'bogus' }
    const wrapper = mount(DeployList, { attachTo: document.body })

    await vi.waitFor(() => expect(document.body.textContent).toContain('dep'))
    expect(
      listDeployments.mock.calls.every(([params]) => params.status === undefined),
    ).toBe(true)
    wrapper.unmount()
  })
})

describe('DeployList 行操作「启动/停止」可见性', () => {
  /** 行内操作按钮文字清单 */
  const actionTexts = (row: Element) =>
    [...row.querySelectorAll('td:last-child button')].map((btn) => btn.textContent ?? '')

  it('failed 行显示「启动」、不显示「停止」;running 行相反', async () => {
    vi.mocked(listDeployments).mockResolvedValue({
      items: [
        makeRow({ deployment_id: 'd1', name: 'dep-failed', status: 'failed' }),
        makeRow({ deployment_id: 'd2', name: 'dep-running', status: 'running' }),
      ],
      total: 2,
      page: 1,
      page_size: 10,
      total_pages: 1,
    })
    const wrapper = mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('dep-running'))

    const rows = [...document.body.querySelectorAll('tbody tr')]
    const failedRow = rows.find((row) => row.textContent?.includes('dep-failed'))!
    const runningRow = rows.find((row) => row.textContent?.includes('dep-running'))!

    // failed 已是停止终态:提供「启动」,不再提供「停止」
    expect(actionTexts(failedRow).some((text) => text.includes('启动'))).toBe(true)
    expect(actionTexts(failedRow).some((text) => text.includes('停止'))).toBe(false)
    expect(actionTexts(runningRow).some((text) => text.includes('停止'))).toBe(true)
    expect(actionTexts(runningRow).some((text) => text.includes('启动'))).toBe(false)
    wrapper.unmount()
  })

  it('点击 failed 行「启动」:确认后调 start 接口并提示成功', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(startDeployment).mockResolvedValue({})
    vi.mocked(listDeployments).mockResolvedValue({
      items: [makeRow({ status: 'failed' })],
      total: 1,
      page: 1,
      page_size: 10,
      total_pages: 1,
    })
    const wrapper = mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('dep'))

    const startBtn = [...document.body.querySelectorAll('tbody tr button')].find((btn) =>
      btn.textContent?.includes('启动'),
    )!
    startBtn.click()

    await vi.waitFor(() => expect(startDeployment).toHaveBeenCalledWith('d1', false))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已启动:dep'))
    wrapper.unmount()
  })
})
