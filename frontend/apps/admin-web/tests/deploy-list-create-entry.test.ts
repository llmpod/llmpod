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
  ...(await importOriginal<typeof import('@/api/deployment')>()),
  createDeployment: vi.fn(),
  deleteDeployment: vi.fn(),
  getDeployment: vi.fn(),
  getDeploymentNodeSummary: vi.fn(),
  listDeploymentLogs: vi.fn(),
  listDeployments: vi.fn(),
  startDeployment: vi.fn(),
  stopDeployment: vi.fn(),
  inspectDeployment: vi.fn(),
}))
vi.mock('@/api/topology', () => ({ getClusterTopology: vi.fn() }))
vi.mock('@/api/inference-image', () => ({
  listInferenceEngines: vi.fn(),
  listInferenceImages: vi.fn(),
}))
vi.mock('@/api/cluster', () => ({ listClusters: vi.fn() }))
vi.mock('@/api/tail-log', () => ({ tailContainerLog: vi.fn() }))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@/views/model-management/add-model/AddModelDialog.vue', () => ({ default: {} }))
vi.mock('@/views/model-management/EditModelDialog.vue', () => ({ default: {} }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useConfirm: vi.fn(),
    useDialog: vi.fn(),
    message: { success: vi.fn(), error: vi.fn() },
  }
})

import { inspectDeployment, listDeployments } from '@/api/deployment'
import { listInferenceEngines, listInferenceImages } from '@/api/inference-image'
import { listClusters } from '@/api/cluster'
import { listModels } from '@/api/model'
import type { InspectDeploymentResult } from '@/api/deployment'
import { useDialog } from '@repo/hooks'
import { useWizardStore } from '@/views/deploy/components/deploy-list/create-wizard/useStore'
import DeployList from '@/views/deploy/components/deploy-list/DeployList.vue'
import ModelManagement from '@/views/model-management/index.vue'
import * as vueRouterMock from 'vue-router'

const route = (vueRouterMock as unknown as { __route: { query: Record<string, unknown> } }).__route
const router = (vueRouterMock as unknown as { __router: { replace: ReturnType<typeof vi.fn> } }).__router

/** 最小合法预检结果(仅 model_id 阶段):推荐留空由 watch 跳过回填 */
function makeInspect(): InspectDeploymentResult {
  return {
    deployable: true,
    issues: [],
    recommended_cluster_id: null,
    cross_node: false,
    recommendation: {
      engine: '',
      image: null,
      topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: null },
      vram_per_gpu_gb: 0,
    },
    cluster_gpus: [],
    node_support: [],
    recommended_placement: null,
    resolved: null,
  } as InspectDeploymentResult
}

beforeEach(() => {
  vi.clearAllMocks()
  route.query = {}
  vi.mocked(listDeployments).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 10,
    total_pages: 0,
  })
  vi.mocked(listModels).mockResolvedValue({
    items: [
      { id: 3, name: 'Qwen2.5-7B' },
      { id: 5, name: 'llama-3-8b' },
    ],
    total: 2,
    page: 1,
    page_size: 100,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listModels>>)
  vi.mocked(listInferenceEngines).mockResolvedValue([])
  vi.mocked(listInferenceImages).mockResolvedValue([])
  vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
  vi.mocked(listClusters).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 100,
    total_pages: 0,
  })
})

afterEach(() => {
  route.query = {}
  document.body.innerHTML = ''
})

describe('DeployList 入口动作「?action=deploy&model=」(模型管理「新建部署」跳入)', () => {
  it('首帧直接渲染向导(不闪列表):工具条与表格不出现,向导标题与步骤条出现', async () => {
    route.query = { action: 'deploy', model: '3', tab: 'list' }
    const wrapper = mount(DeployList, { attachTo: document.body })
    await nextTick()

    // 首帧即向导:列表工具条(新建部署/部署向导按钮)不出现
    const toolbarButtons = [...document.body.querySelectorAll('button')].filter((btn) =>
      btn.textContent?.includes('部署向导'),
    )
    expect(toolbarButtons).toHaveLength(0)
    // 向导标题 + 步骤条出现
    expect(document.body.textContent).toContain('新建部署')
    expect(document.body.textContent).toContain('基本信息')
    // query 清理:action/model 随一次性参数移除,tab 保留
    expect(router.replace).toHaveBeenCalledWith({ query: { tab: 'list' } })
    wrapper.unmount()
  })

  it('入口预选模型:向导 store 复位后回填 modelId', async () => {
    route.query = { action: 'deploy', model: '5' }
    const wrapper = mount(DeployList, { attachTo: document.body })

    const store = useWizardStore()
    await vi.waitFor(() => expect(store.form.modelId).toBe(5))
    wrapper.unmount()
  })

  it('model 缺失时仍打开向导但不预选(回到常规新建)', async () => {
    route.query = { action: 'deploy' }
    const wrapper = mount(DeployList, { attachTo: document.body })
    await nextTick()

    const store = useWizardStore()
    expect(store.form.modelId).toBeNull()
    expect(document.body.textContent).toContain('基本信息')
    wrapper.unmount()
  })

  it('?action=create(全局搜索)仍打开向导,不预选模型', async () => {
    route.query = { action: 'create' }
    const wrapper = mount(DeployList, { attachTo: document.body })
    await nextTick()

    const store = useWizardStore()
    expect(store.form.modelId).toBeNull()
    expect(document.body.textContent).toContain('基本信息')
    expect(router.replace).toHaveBeenCalledWith({ query: {} })
    wrapper.unmount()
  })

  it('无 action 时停留在列表,不打开向导', async () => {
    const wrapper = mount(DeployList, { attachTo: document.body })
    await vi.waitFor(() => expect(document.body.textContent).toContain('还没有部署'))
    expect(document.body.textContent).not.toContain('基本信息')
    wrapper.unmount()
  })
})

describe('模型管理内嵌 tab 与部署入口的 action 隔离', () => {
  it('management 子 tab 挂载着收到 action=deploy 不误开「添加模型」(独立键天然互不干扰)', async () => {
    // 场景回放:用户正停留在 deploy 路由的 management tab(模型管理),点行内「新建部署」
    // 触发同路由 push { tab:'list', action:'deploy' };management 的 useEntryAction({create})
    // 对 deploy 键无 handler,不会误开添加模型弹窗,query 完整送达 list tab 的 DeployList
    route.query = { tab: 'management', action: 'deploy', model: '3' }
    const wrapper = mount(ModelManagement, { attachTo: document.body })
    await nextTick()

    expect(useDialog).not.toHaveBeenCalled()
    // query 未被 management 的入口动作清掉
    expect(router.replace).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
