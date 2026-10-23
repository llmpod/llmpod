// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/cluster', () => ({ listClusters: vi.fn() }))
vi.mock('@/api/deployment', () => ({ inspectDeployment: vi.fn() }))
vi.mock('@/api/inference-image', () => ({
  listInferenceEngines: vi.fn(),
  listInferenceImages: vi.fn(),
}))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@/api/topology', () => ({ getClusterTopology: vi.fn() }))
vi.mock('@/views/deploy/components/deploy-list/useStore', () => ({ useStore: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return { ...actual, message: { warning: vi.fn(), error: vi.fn(), success: vi.fn() } }
})

import { message } from '@repo/hooks'
import { useStore as useDeployListStore } from '@/views/deploy/components/deploy-list/useStore'
import type { ClusterTopology, TopologyGpu, TopologyNode } from '@/api/topology'
import type { InspectDeploymentResult } from '@/api/deployment'
import { useWizardStore } from '@/views/deploy/components/deploy-list/create-wizard/useStore'
import StepConfirm from '@/views/deploy/components/deploy-list/create-wizard/steps/StepConfirm.vue'

const store = useWizardStore()
const createDeploy = vi.fn()

function makeNode(id: string, hostname: string): TopologyNode {
  return { id, cluster_id: 1, hostname, ip: '127.0.0.1', status: 'online' } as TopologyNode
}

function makeGpu(uuid: string, nodeId: string, index: number): TopologyGpu {
  return {
    id: index,
    uuid,
    node_id: nodeId,
    index,
    name: 'A100',
    arch: 'nvidia',
    total_vram_gb: 80,
    available_vram_gb: 80,
    util: 0,
    status: 'online',
  } as TopologyGpu
}

function makeInspect(overrides: Partial<InspectDeploymentResult> = {}): InspectDeploymentResult {
  return {
    deployable: true,
    issues: [],
    recommended_cluster_id: 1,
    cross_node: false,
    recommendation: {
      engine: 'vllm',
      image: 'reg/vllm:0.9.5',
      topology: { tp: 2, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 },
      vram_per_gpu_gb: 40,
    },
    cluster_gpus: [],
    node_support: [],
    recommended_placement: null,
    resolved: null,
    ...overrides,
  } as InspectDeploymentResult
}

function seed() {
  store.form.name = 'qwen2-5-7b-a1b2'
  store.form.modelId = 7
  store.form.clusterId = 1
  store.form.engine = 'auto'
  store.modelOptions.value = [{ label: 'Qwen2.5-7B', value: 7, raw: {} }] as typeof store.modelOptions.value
  store.clusterOptions.value = [{ label: 'gpu-shared', value: 1, raw: { name: 'gpu-shared' } }] as unknown as typeof store.clusterOptions.value
  store.engines.value = [
    { engine: 'vllm', display_name: 'vLLM', deployable: true },
  ] as unknown as typeof store.engines.value
  store.topology.value = {
    cluster: { id: 1, name: 'gpu-shared', type: 'docker', status: 'active' },
    nodes: [makeNode('n1', 'gpu-node-01'), makeNode('n2', 'gpu-node-02')],
    gpus: [makeGpu('gpu-1', 'n1', 0), makeGpu('gpu-2', 'n1', 1), makeGpu('gpu-3', 'n2', 0)],
    deployments: [],
  } as ClusterTopology
  store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-2'])
  store.inspect.value = makeInspect()
}

beforeEach(() => {
  vi.clearAllMocks()
  createDeploy.mockReset()
  vi.mocked(useDeployListStore).mockReturnValue({ createDeploy } as unknown as ReturnType<typeof useDeployListStore>)
  store.reset()
  seed()
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('StepConfirm 确认页回显', () => {
  it('描述列表回显:名称/模型/集群/拓扑/最大上下文/GPU 数', async () => {
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()
    const text = document.body.textContent ?? ''
    expect(text).toContain('qwen2-5-7b-a1b2')
    expect(text).toContain('Qwen2.5-7B')
    expect(text).toContain('gpu-shared')
    expect(text).toContain('2 张')
    // 拓扑摘要按份口径:tp=2 × 副本 1 份,单机不跨节点
    expect(text).toContain('tp=2 · 每副本 2 张 × 1 份 = 2 张 · 单机')
    expect(text).toContain('8192')
    wrapper.unmount()
  })

  it('引擎 auto 时展示推荐标注', async () => {
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()
    expect(document.body.textContent).toContain('自动选择(推荐:vLLM)')
    wrapper.unmount()
  })

  it('拓扑摘要:跨机 PP 形态亮出 pp 与每副本跨节点(节点内 TP + 跨机 PP)', async () => {
    // 2 节点各 2 卡 + 引擎多机 + 词表含 pp(宽松兜底)→ tp=2 · pp=2 · nnodes=2
    store.topology.value = {
      cluster: { id: 1, name: 'gpu-shared', type: 'docker', status: 'active' },
      nodes: [makeNode('n1', 'gpu-node-01'), makeNode('n2', 'gpu-node-02')],
      gpus: [makeGpu('gpu-1', 'n1', 0), makeGpu('gpu-2', 'n1', 1), makeGpu('gpu-3', 'n2', 0), makeGpu('gpu-4', 'n2', 1)],
      deployments: [],
    } as ClusterTopology
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-2', 'gpu-3', 'gpu-4'])
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()
    expect(document.body.textContent).toContain(
      'tp=2 · pp=2 · 每副本 4 张 × 1 份 = 4 张 · 每副本跨 2 节点(节点内 TP + 跨机 PP)',
    )
    wrapper.unmount()
  })

  it('拓扑摘要:跨机 TP 形态标注(词表无 pp 时旧行为)', async () => {
    store.engines.value = [
      { engine: 'vllm', display_name: 'vLLM', deployable: true, supported_parallel: ['tp'] },
    ] as unknown as typeof store.engines.value
    store.topology.value = {
      cluster: { id: 1, name: 'gpu-shared', type: 'docker', status: 'active' },
      nodes: [makeNode('n1', 'gpu-node-01'), makeNode('n2', 'gpu-node-02')],
      gpus: [makeGpu('gpu-1', 'n1', 0), makeGpu('gpu-2', 'n2', 0)],
      deployments: [],
    } as ClusterTopology
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-2'])
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()
    expect(document.body.textContent).toContain(
      'tp=2 · 每副本 2 张 × 1 份 = 2 张 · 每副本跨 2 节点(跨机 TP)',
    )
    wrapper.unmount()
  })

  it('算力明细按节点分组渲染 GPU 标签', async () => {
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()
    expect(document.body.textContent).toContain('gpu-node-01')
    expect(document.body.textContent).toContain('A100 · GPU 0')
    expect(document.body.textContent).toContain('A100 · GPU 1')
    wrapper.unmount()
  })

  it('预检 issues 与跨节点提示展示', async () => {
    store.inspect.value = makeInspect({ issues: ['显存余量不足 10%'], cross_node: true })
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()
    expect(document.body.textContent).toContain('显存余量不足 10%')
    expect(document.body.textContent).toContain('当前配置将跨节点部署')
    wrapper.unmount()
  })
})

describe('StepConfirm 高级参数编辑', () => {
  it('高级参数折叠面板存在(args/env 增删逻辑由 store 单测覆盖)', async () => {
    // n-collapse 在 happy-dom 无法经点击展开(内部测量限制),
    // editArgs/editEnv 的增删改由 deploy-wizard-store 用例覆盖,此处锁定面板结构存在
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()
    expect(document.body.textContent).toContain('高级参数')
    wrapper.unmount()
  })
})

describe('StepConfirm 发布配置', () => {
  it('默认折叠:仅展示标题/开关/详情项,表单字段不渲染', async () => {
    // n-collapse 在 happy-dom 无法经点击展开(内部测量限制),同高级参数用例只锁定面板结构;
    // 表单与关闭提示在展开后按 autoPublish 分支渲染,字段绑定由 deploy-wizard-store 用例覆盖
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()
    const text = document.body.textContent ?? ''
    expect(text).toContain('发布配置')
    // f663f3d 文案改「部署达成后」→「部署完成后」;textContent 在 部署/完成后 之间有换行,断言取后半段
    expect(text).toContain('完成后自动发布')
    expect(text).toContain('发布详情')
    expect(text).not.toContain('对外服务名')
    expect(text).not.toContain('已关闭自动发布')
    wrapper.unmount()
  })
})

// 启动参数 AutoComplete 选项与 naive-ui 的 DOM 级交互无法在 happy-dom 验证:
// 内部菜单 virtualScroll 默认 true,VirtualList 依赖高度测量(全为 0 → 渲染 0 项),
// 与 n-collapse 测量限制同类;render 保留原 node 的结构契约由 deploy-wizard-store 用例锁定。

describe('StepConfirm 提交门控', () => {
  it('validate 成功:submitCreate 提交并提示已创建', async () => {
    createDeploy.mockResolvedValue({ deployment_id: 'd-1' })
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()

    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    await expect(validate()).resolves.toBe(true)
    expect(createDeploy).toHaveBeenCalledTimes(1)
    expect(message.success).toHaveBeenCalledWith('部署 qwen2-5-7b-a1b2 已创建')
    wrapper.unmount()
  })

  it('validate 失败:提交异常返回 false 不提示成功', async () => {
    createDeploy.mockRejectedValue(new Error('409 conflict'))
    const wrapper = mount(StepConfirm, { attachTo: document.body })
    await nextTick()

    const validate = (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
    await expect(validate()).resolves.toBe(false)
    expect(message.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
