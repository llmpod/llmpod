// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Vue Flow 在 happy-dom 无法完整工作(尺寸测量/d3-zoom),mock 组件与组合式 API,
// 只保留 onNodeClick/onSelectionEnd 回调捕获用于驱动交互
const flowState = {
  clickHandlers: [] as Array<(event: { node: { type: string; data: { record: unknown } } }) => void>,
  selectionEndHandler: null as null | (() => void),
  selectedNodes: [] as { type: string; data: { record: unknown } }[],
}

vi.mock('@vue-flow/core', async () => {
  const { ref } = await import('vue')
  return {
    // 渲染各节点卡具名 slot(传整个 node 作 slot props),让 Select*Card 真实参与渲染供文本断言
    VueFlow: {
      name: 'VueFlowStub',
      props: {
        nodes: { type: Array, default: () => [] },
        edges: { type: Array, default: () => [] },
      },
      template: `
        <div class="vue-flow-stub">
          <div v-for="node in nodes" :key="node.id">
            <slot :name="'node-' + node.type" v-bind="{ ...node, zIndex: 0, events: {} }" />
          </div>
        </div>
      `,
    },
    SelectionMode: { Partial: 'partial' },
    useVueFlow: () => ({
      onNodeClick: (cb: (event: never) => void) => flowState.clickHandlers.push(cb),
      onSelectionEnd: (cb: () => void) => {
        flowState.selectionEndHandler = cb
      },
      fitView: vi.fn(),
      getSelectedNodes: { get value() { return flowState.selectedNodes } },
      removeSelectedNodes: vi.fn(),
    }),
    useKeyPress: () => ref(false),
    useNodesInitialized: () => ref(true),
    // NodeCard 内部的连线锚点(happy-dom 下无真实画布,渲染占位即可)
    Handle: { name: 'HandleStub', template: '<div />' },
    Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
  }
})
vi.mock('@vue-flow/background', () => ({ Background: { template: '<div />' } }))
vi.mock('@vue-flow/controls', () => ({ Controls: { template: '<div />' } }))

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
import { inspectDeployment } from '@/api/deployment'
import { getClusterTopology } from '@/api/topology'
import { useStore as useDeployListStore } from '@/views/deploy/components/deploy-list/useStore'
import type { ClusterTopology, TopologyGpu, TopologyNode } from '@/api/topology'
import type { InspectClusterGpu, InspectDeploymentResult } from '@/api/deployment'
import { useWizardStore } from '@/views/deploy/components/deploy-list/create-wizard/useStore'
import StepGpuSelect from '@/views/deploy/components/deploy-list/create-wizard/steps/StepGpuSelect.vue'

const store = useWizardStore()

function makeNode(id: string, status = 'online'): TopologyNode {
  return {
    id,
    cluster_id: 1,
    hostname: `host-${id}`,
    ip: '127.0.0.1',
    status,
    memory_gb: 64,
    available_memory_gb: 60,
    memory_used_gb: 4,
    memory_percent: 6,
    cpu_percent: 12,
  } as TopologyNode
}

function makeGpu(uuid: string, nodeId: string, overrides: Partial<TopologyGpu> = {}): TopologyGpu {
  return {
    id: 1,
    uuid,
    node_id: nodeId,
    index: 0,
    name: 'A100',
    arch: 'nvidia',
    total_vram_gb: 80,
    available_vram_gb: 80,
    util: 0,
    status: 'online',
    ...overrides,
  } as TopologyGpu
}

function seedTopology() {
  store.topology.value = {
    cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
    nodes: [makeNode('n1'), makeNode('n2')],
    gpus: [
      makeGpu('gpu-1', 'n1'),
      makeGpu('gpu-2', 'n1'),
      makeGpu('gpu-4', 'n1'),
      makeGpu('gpu-3', 'n2'),
    ],
    deployments: [],
  } as ClusterTopology
}

function makeInspect(overrides: Partial<InspectDeploymentResult> = {}): InspectDeploymentResult {
  return {
    deployable: true,
    issues: [],
    recommended_cluster_id: 1,
    cross_node: false,
    recommendation: {
      engine: 'vllm',
      image: 'reg/vllm:latest',
      topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: null },
      vram_per_gpu_gb: 40,
    },
    cluster_gpus: [],
    node_support: [],
    recommended_placement: null,
    resolved: null,
    ...overrides,
  } as InspectDeploymentResult
}

function makeAssessed(
  uuid: string,
  nodeId: string,
  overrides: Partial<InspectClusterGpu> = {},
): InspectClusterGpu {
  return {
    uuid,
    node_id: nodeId,
    index: 0,
    arch: 'nvidia',
    name: 'A100',
    driver_version: '570',
    total_vram_gb: 80,
    available_vram_gb: 80,
    status: 'online',
    grey_reason: null,
    grey_detail: null,
    fit: true,
    group_key: 'g-a',
    required_vram_gb: 40,
    ...overrides,
  } as InspectClusterGpu
}

function mountStep() {
  return mount(StepGpuSelect, { attachTo: document.body })
}

/** VueFlow stub 内每个直连 div 对应一张卡,按 graph.nodes 顺序:集群 → 节点 → GPU */
function cardTexts(): string[] {
  return [...document.querySelectorAll('.vue-flow-stub > div')].map((el) => el.textContent ?? '')
}

beforeEach(() => {
  vi.clearAllMocks()
  flowState.clickHandlers = []
  flowState.selectionEndHandler = null
  flowState.selectedNodes = []
  vi.mocked(useDeployListStore).mockReturnValue({ createDeploy: vi.fn() } as unknown as ReturnType<typeof useDeployListStore>)
  store.reset()
  seedTopology()
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('StepGpuSelect 门控与提示(ee61a7a)', () => {
  it('未选卡:canNext=false,tip 提示选卡,validate 拦截并警告', async () => {
    const wrapper = mountStep()
    await nextTick()
    const vm = wrapper.vm as unknown as {
      canNext: boolean
      nextDisabledTip?: string
      validate: () => Promise<boolean>
    }
    expect(vm.canNext).toBe(false)
    expect(vm.nextDisabledTip).toBe('请至少选择一张 GPU(或一个 CPU 节点)')
    await expect(vm.validate()).resolves.toBe(false)
    expect(message.warning).toHaveBeenCalledWith('请至少选择一张 GPU(或一个 CPU 节点)')
    wrapper.unmount()
  })

  it('选卡且拓扑合法:canNext=true,validate 通过', async () => {
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    const wrapper = mountStep()
    await nextTick()
    const vm = wrapper.vm as unknown as { canNext: boolean; validate: () => Promise<boolean> }
    expect(vm.canNext).toBe(true)
    await expect(vm.validate()).resolves.toBe(true)
    wrapper.unmount()
  })

  it('跨机不均分:canNext=false,tip 透传跨机约束文案,validate 拦截', async () => {
    // 4 卡分布 3+1:整除(4/2=2)但各节点不均分
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-2', 'gpu-4', 'gpu-3'])
    const wrapper = mountStep()
    await nextTick()
    const vm = wrapper.vm as unknown as {
      canNext: boolean
      nextDisabledTip?: string
      validate: () => Promise<boolean>
    }
    expect(vm.canNext).toBe(false)
    expect(vm.nextDisabledTip).toContain('各节点卡数一致')
    await expect(vm.validate()).resolves.toBe(false)
    wrapper.unmount()
  })
})

describe('StepGpuSelect 不可选原因展示(grey_detail / node_support)', () => {
  it('GPU 卡灰化展示 grey_detail 人读详情,节点卡无可选展示 node_support 原因', async () => {
    store.inspect.value = makeInspect({
      cluster_gpus: [
        makeAssessed('gpu-1', 'n1', {
          grey_reason: 'no_image',
          grey_detail: 'vLLM 对 A100(驱动 570)无匹配镜像',
        }),
        makeAssessed('gpu-3', 'n2', {
          grey_reason: 'no_image',
          grey_detail: 'vLLM 对 A100(驱动 570)无匹配镜像',
        }),
      ],
      node_support: [
        { node_id: 'n2', hostname: 'host-n2', selectable: false, reason: '节点无引擎可用 GPU' },
      ],
    })
    const wrapper = mountStep()
    await nextTick()
    const texts = cardTexts()
    // gpu-1 卡(seedTopology 顺序:cluster,n1,n2,gpu-1,gpu-2,gpu-4,gpu-3):不可选 + grey_detail
    expect(texts[3]).toContain('不可选')
    expect(texts[3]).toContain('vLLM 对 A100(驱动 570)无匹配镜像')
    // n2 节点卡:其下卡全灰 → 展示 node_support 原因
    expect(texts[2]).toContain('节点无引擎可用 GPU')
    // n1 节点卡:gpu-2/gpu-4 无评估仍可选,不进入不可选分支
    expect(texts[1]).not.toContain('节点无引擎可用 GPU')
    // 原因文本红色 + truncate,承载 grey_detail / node_support 原因
    const errorEls = [...document.querySelectorAll('.text-error')]
    expect(errorEls.map((el) => el.textContent)).toEqual(
      expect.arrayContaining(['vLLM 对 A100(驱动 570)无匹配镜像', '节点无引擎可用 GPU']),
    )
    expect(errorEls.every((el) => el.className.includes('truncate'))).toBe(true)
    wrapper.unmount()
  })

  it('节点离线回退状态文案,在线但无 node_support 记录回退「无可选 GPU」', async () => {
    store.topology.value = {
      cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
      nodes: [makeNode('n1'), makeNode('n2', 'offline')],
      gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-3', 'n2')],
      deployments: [
        {
          deployment_id: 'd1',
          cluster_id: 1,
          name: 'dep',
          status: 'running',
          phase: 'RUNNING',
          gpu_uuids: ['gpu-1'],
        } as ClusterTopology['deployments'][number],
      ],
    } as ClusterTopology
    store.inspect.value = makeInspect({ cluster_gpus: [], node_support: [] })
    const wrapper = mountStep()
    await nextTick()
    const texts = cardTexts()
    // n1 在线,唯一卡被运行中部署占用:整选批量口径无可选 → 节点卡回退兜底文案
    // (被占卡本身手选可选,不误报节点不可选)
    expect(texts[1]).toContain('无可选 GPU')
    // n2 离线:状态行与不可选原因行均为「离线」,至少出现两处证明原因行已渲染
    expect((texts[2].match(/离线/g) ?? []).length).toBeGreaterThanOrEqual(2)
    // gpu-1:手选口径可选,不再渲染红色「不可选」;占用走橙色告警行(inspect 无记录回退通用短句)
    expect(texts[3]).toContain('已被部署占用,可手选共用显存')
    expect(texts[3]).not.toContain('不可选')
    const errorTexts = [...document.querySelectorAll('.text-error')].map(
      (el) => el.textContent ?? '',
    )
    expect(errorTexts).toEqual(expect.arrayContaining(['节点不在线', '离线']))
    expect(errorTexts).not.toContain('已被部署占用')
    wrapper.unmount()
  })
})

describe('StepGpuSelect 选卡交互', () => {
  it('全选/清空按钮驱动 store(单副本:全选入唯一份;清空清全部)', async () => {
    const wrapper = mountStep()
    await nextTick()
    const findBtn = (text: string) =>
      [...document.querySelectorAll('button')].find((btn) => btn.textContent?.includes(text))

    // 单副本:按钮文案为「全选」(多副本才升级为「自动分配」)
    const allBtn = findBtn('全选')
    expect(allBtn).toBeTruthy()
    allBtn!.click()
    await nextTick()
    expect(store.selectedGpuUuids.value.size).toBe(4)
    expect(store.allSelectedUuids.value.size).toBe(4)

    // 清空为独立按钮:清掉所有份的选卡
    const clearBtn = findBtn('清空')
    expect(clearBtn).toBeTruthy()
    clearBtn!.click()
    await nextTick()
    expect(store.allSelectedUuids.value.size).toBe(0)
    wrapper.unmount()
  })

  it('节点卡点击整选,GPU 卡点击单选', async () => {
    const wrapper = mountStep()
    await nextTick()
    const onNodeClick = flowState.clickHandlers[0]
    expect(onNodeClick).toBeTruthy()

    onNodeClick({ node: { type: 'node', data: { record: store.topology.value!.nodes[0] } } })
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['gpu-1', 'gpu-2', 'gpu-4'])

    onNodeClick({ node: { type: 'gpu', data: { record: store.topology.value!.gpus[3] } } })
    expect(store.selectedGpuUuids.value.has('gpu-3')).toBe(true)
    wrapper.unmount()
  })

  it('llama.cpp:已选某节点卡后其它节点卡灰化并展示单节点原因', async () => {
    store.form.engine = 'llamacpp'
    store.selectedGpuUuids.value = new Set(['gpu-1']) // n1
    const wrapper = mountStep()
    await nextTick()
    // 卡片顺序:cluster,n1,n2,gpu-1,gpu-2,gpu-4,gpu-3 → 末位是 n2 的 gpu-3
    const texts = [...document.querySelectorAll('.vue-flow-stub > div')].map((el) => el.textContent ?? '')
    expect(texts[6]).toContain('llama.cpp 仅支持单节点部署')
    expect(texts[6]).toContain('n1')
    wrapper.unmount()
  })

  it('切到 llama.cpp:残留跨节点选集收敛到单节点', async () => {
    store.form.engine = 'vllm'
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-3']) // n1 + n2
    const wrapper = mountStep()
    await nextTick()
    expect(store.selectedGpuUuids.value.size).toBe(2)

    store.form.engine = 'llamacpp'
    await nextTick()
    expect([...store.selectedGpuUuids.value]).toEqual(['gpu-1'])
    wrapper.unmount()
  })

  it('CPU 落点:有无可用卡的节点卡片都显示 CPU 部署,点卡片选中 CPU 部署并在摘要回显', async () => {
    store.engines.value = [
      {
        engine: 'llamacpp',
        display_name: 'llama.cpp',
        deployable: true,
        supported_gpu_archs: ['nvidia', 'cpu'],
        kv_cache_dtype_options: [],
      },
    ] as unknown as typeof store.engines.value
    store.form.engine = 'llamacpp'
    store.inspect.value = makeInspect({
      node_support: [
        {
          node_id: 'n1',
          hostname: 'host-n1',
          selectable: false,
          reason: '节点 GPU 架构不被该引擎支持',
          memory_gb: 64,
          available_memory_gb: 60,
        },
        {
          node_id: 'n2',
          hostname: 'host-n2',
          selectable: true,
          reason: null,
          memory_gb: 128,
          available_memory_gb: 100,
        },
      ],
    })
    const wrapper = mountStep()
    await nextTick()
    // 节点卡(顺序:cluster,n1,n2)都带 CPU 部署入口(有可用卡的机器同样支持纯 CPU)
    const texts = [...document.querySelectorAll('.vue-flow-stub > div')].map((el) => el.textContent ?? '')
    expect(texts[1]).toContain('CPU 部署')
    expect(texts[2]).toContain('CPU 部署')
    // 实时负载行:CPU 使用率 + 内存可用/总量(选节点参考)
    expect(texts[1]).toContain('CPU')
    expect(texts[1]).toContain('12%')
    expect(texts[1]).toContain('60.0/64.0GB')

    const onNodeClick = flowState.clickHandlers[0]
    onNodeClick({ node: { type: 'node', data: { record: store.topology.value!.nodes[0] } } })
    await nextTick()
    expect(store.selectedCpuNodeId.value).toBe('n1')
    expect(document.body.textContent).toContain('CPU 部署节点')
    expect(document.body.textContent).toContain('host-n1')
    // 选中后卡片回显已选态(激活份钉定)
    const afterSelect = [...document.querySelectorAll('.vue-flow-stub > div')].map(
      (el) => el.textContent ?? '',
    )
    expect(afterSelect[1]).toContain('CPU 部署 · 本副本已选')
    wrapper.unmount()
  })

  it('有可用卡的节点:点卡片走纯 CPU 部署(不选卡)', async () => {
    store.engines.value = [
      {
        engine: 'llamacpp',
        display_name: 'llama.cpp',
        deployable: true,
        supported_gpu_archs: ['nvidia', 'cpu'],
        kv_cache_dtype_options: [],
      },
    ] as unknown as typeof store.engines.value
    store.form.engine = 'llamacpp'
    const wrapper = mountStep()
    await nextTick()

    const onNodeClick = flowState.clickHandlers[0]
    onNodeClick({ node: { type: 'node', data: { record: store.topology.value!.nodes[1] } } })
    await nextTick()
    expect(store.selectedCpuNodeId.value).toBe('n2')
    expect(store.selectedGpuUuids.value.size).toBe(0)
    wrapper.unmount()
  })

  it('框选结束:选区 GPU 并入选中集,Vue Flow 选中态清除', async () => {    const wrapper = mountStep()
    await nextTick()
    flowState.selectedNodes = [
      { type: 'gpu', data: { record: store.topology.value!.gpus[0] } },
      { type: 'node', data: { record: store.topology.value!.nodes[0] } },
    ]
    flowState.selectionEndHandler!()
    expect(store.selectedGpuUuids.value.has('gpu-1')).toBe(true)
    wrapper.unmount()
  })

  it('已选摘要按节点分组,关闭 tag 移除该节点已选', async () => {
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-3'])
    const wrapper = mountStep()
    await nextTick()
    expect(document.body.textContent).toContain('host-n1 × 1')
    expect(document.body.textContent).toContain('host-n2 × 1')

    const closeBtn = [...document.querySelectorAll('.n-tag')].find((tag) =>
      tag.textContent?.includes('host-n1'),
    )?.querySelector('.n-tag__close, .n-base-close')
    expect(closeBtn).toBeTruthy()
    ;(closeBtn as HTMLElement).click()
    await nextTick()
    expect(store.selectedGpuUuids.value.has('gpu-1')).toBe(false)
    expect(store.selectedGpuUuids.value.has('gpu-3')).toBe(true)
    wrapper.unmount()
  })

  it('预检失败展示降级提示,重试触发全量预检(force)', async () => {
    store.form.modelId = 7
    store.form.clusterId = 1
    store.inspectError.value = true
    const wrapper = mountStep()
    await nextTick()
    expect(document.body.textContent).toContain('部署预检加载失败')

    const retryBtn = [...document.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('重试'),
    )!
    retryBtn.click()
    await nextTick()
    // 组件实例持有自己的 store 闭包(useWizardStore 为工厂),经 api 层断言重试行为
    expect(inspectDeployment).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('预检推荐摘要:显存需求 + 推荐引擎/拓扑', async () => {
    store.inspect.value = makeInspect({
      cluster_gpus: [
        {
          uuid: 'gpu-1',
          node_id: 'n1',
          index: 0,
          arch: 'nvidia',
          name: 'A100',
          driver_version: '570',
          total_vram_gb: 80,
          available_vram_gb: 80,
          status: 'online',
          grey_reason: null,
          fit: true,
          group_key: 'g-a',
          required_vram_gb: 40,
        },
      ],
    })
    store.engines.value = [
      { engine: 'vllm', display_name: 'vLLM', deployable: true },
    ] as unknown as typeof store.engines.value
    const wrapper = mountStep()
    await nextTick()
    expect(document.body.textContent).toContain('模型约需显存 40GB')
    expect(document.body.textContent).toContain('推荐引擎 vLLM')
    expect(document.body.textContent).toContain('推荐 tp=1')
    wrapper.unmount()
  })

  it('推荐选卡为空(副本超出空闲卡):报错透传后端 issues 分流文案', async () => {
    store.form.replicas = 3
    store.inspect.value = makeInspect({
      deployable: false,
      issues: ['当前 tp=1×replicas=3 需要更多满足显存的卡,集群空闲卡不足(单卡最大可用 19.3GB 已够);请释放占用卡或缩小并行度'],
      recommended_placement: {
        gpu_uuids: [],
        node_ids: [],
        arch: 'nvidia',
        total_gpus: 3,
        arch_summary: {},
        unit_plans: [],
        replica_plans: [],
      },
    })
    const wrapper = mountStep()
    await nextTick()
    expect(store.noPlacementReason.value).toBeTruthy()
    expect(document.body.textContent).toContain('replicas=3 需要更多满足显存的卡')
    wrapper.unmount()
  })

  it('推荐选卡为空且 issues 无分流文案:按请求拓扑口径兜底提示', async () => {
    store.form.replicas = 2
    store.inspect.value = makeInspect({
      issues: [],
      recommended_placement: {
        gpu_uuids: [],
        node_ids: [],
        arch: 'nvidia',
        total_gpus: 2,
        arch_summary: {},
        unit_plans: [],
        replica_plans: [],
      },
    })
    const wrapper = mountStep()
    await nextTick()
    expect(document.body.textContent).toContain('当前副本配置共需 2 张卡,集群空闲可选卡不足')
    wrapper.unmount()
  })

  it('已手选卡时不显示空推荐报错(配不齐由配对预览接管)', async () => {
    store.form.replicas = 3
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-2'])
    store.inspect.value = makeInspect({
      recommended_placement: {
        gpu_uuids: [],
        node_ids: [],
        arch: 'nvidia',
        total_gpus: 3,
        arch_summary: {},
        unit_plans: [],
        replica_plans: [],
      },
    })
    const wrapper = mountStep()
    await nextTick()
    expect(store.noPlacementReason.value).toBeNull()
    expect(document.body.textContent).not.toContain('集群空闲可选卡不足')
    wrapper.unmount()
  })

  it('拓扑失败展示错误覆盖层,重试强制重拉拓扑', async () => {
    store.form.clusterId = 1
    store.topology.value = null
    store.topologyError.value = true
    const wrapper = mountStep()
    await nextTick()
    expect(document.body.textContent).toContain('拓扑加载失败')

    const retryBtn = [...document.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('重试'),
    )!
    retryBtn.click()
    await nextTick()
    expect(getClusterTopology).toHaveBeenCalledWith(1)
    wrapper.unmount()
  })

  it('集群无节点展示空态', async () => {
    store.topology.value = {
      cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
      nodes: [],
      gpus: [],
      deployments: [],
    } as ClusterTopology
    const wrapper = mountStep()
    await nextTick()
    expect(document.body.textContent).toContain('该集群暂无节点')
    wrapper.unmount()
  })

  it('有节点无 GPU 且引擎不支持 CPU:展示空态并说明原因', async () => {
    store.topology.value = {
      cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
      nodes: [makeNode('n1')],
      gpus: [],
      deployments: [],
    } as ClusterTopology
    const wrapper = mountStep()
    await nextTick()
    expect(document.body.textContent).toContain('该集群暂无 GPU,当前引擎不支持 CPU 部署')
    wrapper.unmount()
  })

  it('有节点无 GPU 且引擎声明 cpu:不算空态,节点卡可点走纯 CPU 部署', async () => {
    store.engines.value = [
      {
        engine: 'llamacpp',
        display_name: 'llama.cpp',
        deployable: true,
        supported_gpu_archs: ['nvidia', 'cpu'],
        kv_cache_dtype_options: [],
      },
    ] as unknown as typeof store.engines.value
    store.form.engine = 'llamacpp'
    store.topology.value = {
      cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [],
      deployments: [],
    } as ClusterTopology
    const wrapper = mountStep()
    await nextTick()
    // 接口没返回 GPU 但有 CPU 落点:不再被空态覆盖层盖住,节点卡可见且带 CPU 部署入口
    expect(document.querySelector('.n-empty')).toBeNull()
    const texts = [...document.querySelectorAll('.vue-flow-stub > div')].map(
      (el) => el.textContent ?? '',
    )
    expect(texts[1]).toContain('CPU 部署')
    // 点节点卡选中 CPU 落点 → cpuMode 放行下一步
    flowState.clickHandlers[0]({
      node: { type: 'node', data: { record: store.topology.value!.nodes[0] } },
    })
    await nextTick()
    expect(store.selectedCpuNodeId.value).toBe('n1')
    const vm = wrapper.vm as unknown as { canNext: boolean }
    expect(vm.canNext).toBe(true)
    wrapper.unmount()
  })

  // ---------- 纯 CPU 档落点对接(2026-09-28 inspect recommended_placement 节点级落点) ----------

  /** 纯 CPU 档预检:cpu_only 场景 + binpack 落点 + 逐节点内存两列 */
  function cpuOnlyInspect(overrides: Partial<InspectDeploymentResult> = {}) {
    store.inspect.value = makeInspect({
      memory: {
        weight_gb: 10,
        kv_gb: 2,
        total_layers: 8,
        tiers: [{ offload_layers: 0, ram_gb: 12, vram_gb: 0, feasible: true }],
        scenario: 'cpu_only',
        recommended_tier: 0,
        vram_avail_gb: 0,
        ram_avail_gb: 60,
        kv_source: 'formula',
      },
      node_support: [
        {
          node_id: 'n1',
          hostname: 'host-n1',
          selectable: true,
          reason: null,
          cpu_lane: true,
          memory_gb: 64,
          available_memory_gb: 60,
        },
        {
          node_id: 'n2',
          hostname: 'host-n2',
          selectable: true,
          reason: null,
          memory_gb: 64,
          available_memory_gb: 50,
        },
      ],
      recommended_placement: {
        gpu_uuids: [],
        node_ids: ['n1'],
        arch: 'cpu',
        total_gpus: 0,
        arch_summary: {},
        unit_plans: [],
        replica_plans: [
          {
            seq: 1,
            node_ids: ['n1'],
            arch: 'cpu',
            units: [{ role: 'full', gpu_uuids: [], fit: true, headroom_gb: null }],
            fit: true,
            issues: [],
          },
        ],
        cards: [],
      },
      ...overrides,
    })
  }

  function seedLlamaCppEngine() {
    store.engines.value = [
      {
        engine: 'llamacpp',
        display_name: 'llama.cpp',
        deployable: true,
        supported_gpu_archs: ['nvidia', 'cpu'],
        kv_cache_dtype_options: [],
      },
    ] as unknown as typeof store.engines.value
    store.form.engine = 'llamacpp'
  }

  it('纯 CPU 部署摘要:逐份分组节点 + 每份内存需求;推荐摘要走 CPU 口径', async () => {
    seedLlamaCppEngine()
    cpuOnlyInspect()
    store.toggleCpuNode(store.topology.value!.nodes[0])
    const wrapper = mountStep()
    await nextTick()
    const text = document.body.textContent ?? ''
    expect(text).toContain('CPU 部署节点')
    expect(text).toContain('host-n1')
    expect(text).toContain('1 份已钉')
    // 每份内存需求(0 档 12GB,cpu_only 口径已含 KV)
    expect(text).toContain('每份需约 12GB 内存')
    // 推荐摘要:cpu_only 档不展示显存/tp,走内存 + 落点口径
    expect(text).toContain('CPU 部署 · 需约 12GB 内存')
    expect(text).toContain('推荐落点 1 个节点')
    expect(text).not.toContain('模型约需显存')
    expect(text).not.toContain('推荐 tp=')
    wrapper.unmount()
  })

  it('纯 CPU 档空落点:顶部报错透传后端容量文案,cpuMode 下也阻断下一步', async () => {
    seedLlamaCppEngine()
    cpuOnlyInspect({
      deployable: false,
      issues: ['节点 n1 可用内存 6GB,不足纯 CPU 部署需求 12GB'],
      recommended_placement: {
        gpu_uuids: [],
        node_ids: [],
        arch: 'cpu',
        total_gpus: 0,
        arch_summary: {},
        unit_plans: [],
        replica_plans: [],
        cards: [],
      },
    })
    store.toggleCpuNode(store.topology.value!.nodes[0])
    const wrapper = mountStep()
    await nextTick()
    // cpuMode 下不再压制空落点报错(此前 CPU 部署不受影响口径已过时)
    expect(document.body.textContent).toContain('不足纯 CPU 部署需求')
    const vm = wrapper.vm as unknown as {
      canNext: boolean
      nextDisabledTip?: string
      validate: () => Promise<boolean>
    }
    expect(vm.canNext).toBe(false)
    expect(vm.nextDisabledTip).toContain('不足纯 CPU 部署需求')
    await expect(vm.validate()).resolves.toBe(false)
    expect(message.warning).toHaveBeenCalledWith(
      expect.stringContaining('不足纯 CPU 部署需求'),
    )
    wrapper.unmount()
  })

  it('纯 CPU 多副本:副本卡片摘要按逐份钉定显示落点,未钉显示未选节点', async () => {
    seedLlamaCppEngine()
    cpuOnlyInspect()
    store.form.replicas = 2
    store.toggleCpuNode(store.topology.value!.nodes[0]) // 份 1 钉 n1;份 2 未钉
    const wrapper = mountStep()
    await nextTick()
    const summaries = [...document.querySelectorAll('.replica-card')]
      .map((el) => el.textContent ?? '')
    expect(summaries.length).toBe(2)
    // 摘要按本地钉定态:份 1 显示节点名,份 2 提示未选
    expect(summaries[0]).toContain('CPU 部署 · host-n1')
    expect(summaries[1]).toContain('未选节点')
    // 切到份 2 再钉 n2:互不覆盖
    ;([...document.querySelectorAll('.replica-card')][1] as HTMLElement).click()
    await nextTick()
    store.toggleCpuNode(store.topology.value!.nodes[1])
    await nextTick()
    expect(store.cpuNodeSelections.value).toEqual(['n1', 'n2'])
    // 节点卡颜色区分(与 GPU 卡色环同口径):激活份(份 2)钉定的 n2 卡边框带份 2 颜色,
    // 非激活份(份 1)钉定的 n1 卡带份 1 颜色;份色来自 REPLICA_COLORS(#2080f0 / #7c3aed)
    const nodeWraps = [...document.querySelectorAll('.vue-flow-stub > div')]
    const cardBorder = (wrapIndex: number) => {
      const card = nodeWraps[wrapIndex].querySelector('.rounded-lg') as HTMLElement
      return card.style.borderColor.toLowerCase()
    }
    expect(cardBorder(1)).toMatch(/#2080f0|rgb\(32, ?128, ?240\)/)
    expect(cardBorder(2)).toMatch(/#7c3aed|rgb\(124, ?58, ?237\)/)
    wrapper.unmount()
  })
})

describe('StepGpuSelect 显存共用(被占卡手选解锁 + 批量排除)', () => {
  /** running 部署占 gpu-1/gpu-2 + stopped 部署占 gpu-3 的拓扑 */
  function seedOccupiedTopology() {
    store.topology.value = {
      cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1'), makeGpu('gpu-4', 'n1'), makeGpu('gpu-3', 'n2')],
      deployments: [
        {
          deployment_id: 'd-run',
          cluster_id: 1,
          name: 'dep-running',
          status: 'running',
          phase: 'RUNNING',
          gpu_uuids: ['gpu-1', 'gpu-2'],
        },
        {
          deployment_id: 'd-stop',
          cluster_id: 1,
          name: 'dep-stopped',
          status: 'stopped',
          phase: 'STOPPED',
          gpu_uuids: ['gpu-3'],
        },
      ] as ClusterTopology['deployments'],
    } as ClusterTopology
  }

  it('store:被运行中部署占用的卡手选可选、批量口径不可选;stopped 不算占用', () => {
    seedOccupiedTopology()
    const gpu1 = store.topology.value!.gpus[0] // running 占用
    const gpu3 = store.topology.value!.gpus[3] // stopped「占用」→ 不算
    // 手选口径:被占卡正常可选(共用显存解锁)
    expect(store.isGpuSelectable(gpu1)).toBe(true)
    expect(store.occupiedGpuUuids.value.has('gpu-1')).toBe(true)
    expect(store.occupiedGpuUuids.value.has('gpu-3')).toBe(false)
    expect(store.isGpuSelectable(gpu3)).toBe(true)
    // 批量口径:被占卡不可选(全选/自动分配/框选/整选候选排除)
    expect(store.isGpuBatchSelectable(gpu1)).toBe(false)
    expect(store.isGpuBatchSelectable(gpu3)).toBe(true)
  })

  it('组件:被占卡正常亮度可点选,卡片渲染橙色占用告警行而非不可选', async () => {
    seedOccupiedTopology()
    store.inspect.value = makeInspect({
      cluster_gpus: [
        makeAssessed('gpu-1', 'n1', {
          declared_vram_gb: 13.2,
          occupied_pct: 0.6,
          occupied_by: [{ deployment_id: 'd-run', deployment_name: 'dep-running', declared_gb: 13.2 }],
        }),
        makeAssessed('gpu-2', 'n1', {
          declared_vram_gb: 6.6,
          occupied_pct: 0.3,
          occupied_by: [{ deployment_id: 'd-run', deployment_name: 'dep-running', declared_gb: 6.6 }],
        }),
      ],
    })
    const wrapper = mountStep()
    await nextTick()
    const texts = cardTexts()
    // 卡片顺序:cluster,n1,n2,gpu-1,gpu-2,gpu-4,gpu-3
    // gpu-1:正常信息展示(显存行)+ 橙色占用告警行,无「不可选」
    expect(texts[3]).toContain('显存')
    expect(texts[3]).toContain('占用')
    expect(texts[3]).toContain('已被 dep-running 占用 13.2GB,剩余 58.8GB 可选用')
    expect(texts[3]).not.toContain('不可选')
    // gpu-3(仅 stopped 部署):不渲染占用告警
    expect(texts[6]).not.toContain('已被部署占用')
    // 告警文本橙色(text-warning)
    const warningEls = [...document.querySelectorAll('.text-warning')].map((el) => el.textContent ?? '')
    expect(warningEls).toContain('已被 dep-running 占用 13.2GB,剩余 58.8GB 可选用')
    // 点被占卡可正常选中(手选解锁)
    const onNodeClick = flowState.clickHandlers[0]
    onNodeClick({ node: { type: 'gpu', data: { record: store.topology.value!.gpus[0] } } })
    expect(store.selectedGpuUuids.value.has('gpu-1')).toBe(true)
    wrapper.unmount()
  })

  it('inspect 未加载:被占卡回退通用短句,仍可手选', async () => {
    seedOccupiedTopology()
    store.inspect.value = makeInspect({ cluster_gpus: [] })
    const wrapper = mountStep()
    await nextTick()
    const texts = cardTexts()
    expect(texts[3]).toContain('已被部署占用,可手选共用显存')
    expect(texts[3]).not.toContain('不可选')
    const onNodeClick = flowState.clickHandlers[0]
    onNodeClick({ node: { type: 'gpu', data: { record: store.topology.value!.gpus[0] } } })
    expect(store.selectedGpuUuids.value.has('gpu-1')).toBe(true)
    wrapper.unmount()
  })

  it('批量入口排除被占卡:全选不选入被占卡,节点整选只收未被占用的', async () => {
    seedOccupiedTopology()
    const wrapper = mountStep()
    await nextTick()
    // 全选:gpu-1/gpu-2(被 running 占用)不入选;gpu-4 + gpu-3(stopped 不算占用)入选
    store.selectAll()
    expect([...store.allSelectedUuids.value].sort()).toEqual(['gpu-3', 'gpu-4'])
    // 节点 n1 整选:其下 gpu-1/gpu-2 被占不收,gpu-4 收
    store.clearSelection(false)
    store.toggleNode(store.topology.value!.nodes[0])
    expect([...store.selectedGpuUuids.value]).toEqual(['gpu-4'])
    // 框选:选区内被占卡不入
    store.clearSelection(false)
    flowState.selectedNodes = store.topology.value!.gpus.slice(0, 3).map((record) => ({
      type: 'gpu',
      data: { record },
    }))
    flowState.selectionEndHandler!()
    expect([...store.allSelectedUuids.value].sort()).toEqual(['gpu-4'])
    wrapper.unmount()
  })

  it('空推荐提示:集群有被占可共用卡时,报错末尾追加手选出路', async () => {
    seedOccupiedTopology()
    store.inspect.value = makeInspect({
      deployable: false,
      issues: ['当前 tp=1×replicas=1 需要更多满足显存的卡,集群空闲卡不足;请释放占用卡或缩小并行度'],
      recommended_placement: {
        gpu_uuids: [],
        node_ids: [],
        arch: 'nvidia',
        total_gpus: 1,
        arch_summary: {},
        unit_plans: [],
        replica_plans: [],
      },
      cluster_gpus: [
        makeAssessed('gpu-1', 'n1', {
          declared_vram_gb: 13.2,
          occupied_pct: 0.6,
          occupied_by: [{ deployment_id: 'd-run', deployment_name: 'dep-running', declared_gb: 13.2 }],
        }),
        makeAssessed('gpu-2', 'n1', {
          declared_vram_gb: 6.6,
          occupied_pct: 0.3,
          occupied_by: [{ deployment_id: 'd-run', deployment_name: 'dep-running', declared_gb: 6.6 }],
        }),
      ],
    })
    const wrapper = mountStep()
    await nextTick()
    expect(document.body.textContent).toContain(
      '也可手动选择被占用的 2 张卡共用显存(确认部署时调低显存占比)',
    )
    wrapper.unmount()
  })
})

describe('StepGpuSelect 部署规格(副本卡片/最大上下文)', () => {
  /** 最大上下文 n-input-number(部署规格区唯一数字输入;副本份数改由卡片增删表达) */
  function maxLenInput(wrapper: ReturnType<typeof mountStep>) {
    const inputs = wrapper.findAllComponents({ name: 'InputNumber' })
    expect(inputs.length, '部署规格输入个数').toBe(1)
    return inputs[0]
  }

  /**
   * 最大上下文输入框 suffix 内的下拉箭头(快捷档触发器)
   */
  const maxLenSuffixTrigger = () =>
    document
      .querySelector('.n-input-number')
      ?.querySelector('.n-input__suffix .n-icon') as HTMLElement

  /** 副本卡片元素列表(含序号/摘要/移除图标) */
  const replicaCardEls = () => [...document.querySelectorAll('.replica-card')]

  /** 添加副本按钮 */
  const addReplicaBtn = () =>
    [...document.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('添加副本'),
    )

  it('默认单份副本卡片;添加副本新增一份并激活,画布选卡落在新份', async () => {
    const wrapper = mountStep()
    await nextTick()
    expect(replicaCardEls().length).toBe(1)
    expect(replicaCardEls()[0].textContent).toContain('副本1')

    addReplicaBtn()!.click()
    await nextTick()
    expect(store.replicaSelections.value.length).toBe(2)
    expect(store.activeReplicaIndex.value).toBe(1)
    expect(replicaCardEls().length).toBe(2)
    expect(replicaCardEls()[1].className).toContain('border-primary')

    // 激活份 2:画布选卡写入新份,份 1 不受影响
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[0] } },
    })
    expect([...store.replicaSelections.value[1]]).toEqual(['gpu-1'])
    expect(store.replicaSelections.value[0]).toEqual([])
    wrapper.unmount()
  })

  it('切换激活副本:画布选卡目标随之切换,已选角标归属展示', async () => {
    const wrapper = mountStep()
    await nextTick()
    // 份 1 选 gpu-1
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[0] } },
    })
    addReplicaBtn()!.click()
    await nextTick()
    // 激活份 2:gpu-1 已属份 1 → 不可选;点 gpu-3 入份 2
    expect(store.isGpuSelectable(store.topology.value!.gpus[0])).toBe(false)
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[3] } },
    })
    expect([...store.replicaSelections.value[1]].sort()).toEqual(['gpu-3'])

    // 切回份 1:gpu-3 现在不可选(已属份 2)
    replicaCardEls()[0].click()
    await nextTick()
    expect(store.activeReplicaIndex.value).toBe(0)
    expect(store.isGpuSelectable(store.topology.value!.gpus[3])).toBe(false)
    // 全量并集:两份合计 2 张
    expect(store.allSelectedUuids.value.size).toBe(2)
    wrapper.unmount()
  })

  it('多副本约束:其它份的卡禁选并提示归属;激活份跨节点/架构的卡禁选', async () => {
    const wrapper = mountStep()
    await nextTick()
    // 份 1 选 n1 的 gpu-1
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[0] } },
    })
    addReplicaBtn()!.click()
    await nextTick()
    // gpu-1 已属份 1:份 2 视角禁选 + 原因文案
    expect(store.inspectDisabledReason(store.topology.value!.gpus[0])).toBe('已被副本1选中')
    // 份 2 选 n2 的 gpu-3 后,n1 的 gpu-2 与份 2 桶不一致 → 禁选
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[3] } },
    })
    expect(store.inspectDisabledReason(store.topology.value!.gpus[1])).toBe(
      '须与本副本已选卡同节点同架构',
    )
    expect(store.isGpuSelectable(store.topology.value!.gpus[1])).toBe(false)
    wrapper.unmount()
  })

  it('多副本等量校验:份未选满/卡数不一致禁用下一步并提示', async () => {
    store.form.modelId = 7
    store.form.clusterId = 1
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
    const wrapper = mountStep()
    await nextTick()
    // 份 1 选 n1 的 gpu-1
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[0] } },
    })
    addReplicaBtn()!.click()
    await nextTick()

    const vm = wrapper.vm as unknown as { canNext: boolean; nextDisabledTip?: string }
    // 份 2 未选卡:阻断
    expect(vm.canNext).toBe(false)
    expect(vm.nextDisabledTip).toContain('副本2 还未选卡')

    // 份 2 选 n1 的 gpu-2:各份 1 张等量 → 放行,tp=1 × 2 份
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[1] } },
    })
    await nextTick()
    expect(vm.canNext).toBe(true)
    expect(store.derivedTopology.value).toMatchObject({ tp: 1, replicas: 2, nnodes: 1 })

    // 份 1 补第 2 张(gpu-4):2 vs 1 卡数不一致 → 再阻断
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[2] } },
    })
    await nextTick()
    expect(vm.canNext).toBe(false)
    expect(vm.nextDisabledTip).toContain('卡数须一致')
    wrapper.unmount()
  })

  it('移除副本:份内有卡时二次确认,确认后移除并迁移激活指针', async () => {
    const wrapper = mountStep()
    await nextTick()
    flowState.clickHandlers[0]({
      node: { type: 'gpu', data: { record: store.topology.value!.gpus[0] } },
    })
    addReplicaBtn()!.click()
    await nextTick()
    expect(store.replicaSelections.value.length).toBe(2)

    // 移除空的份 2:直接移除,无确认
    const removeIcon = replicaCardEls()[1].querySelector('.n-icon')
    expect(removeIcon).toBeTruthy()
    ;(removeIcon as HTMLElement).click()
    await nextTick()
    // useConfirm 未被 mock,空份直接移除;份数回到 1,激活回到 0
    expect(store.replicaSelections.value.length).toBe(1)
    expect(store.activeReplicaIndex.value).toBe(0)
    wrapper.unmount()
  })

  it('添加副本:防抖重检带新份数,空份自动回填推荐落点(已选份不覆盖)', async () => {
    store.form.modelId = 7
    store.form.clusterId = 1
    vi.mocked(inspectDeployment).mockResolvedValue(
      makeInspect({
        recommended_placement: {
          gpu_uuids: ['gpu-1', 'gpu-3'],
          node_ids: ['n1', 'n2'],
          arch: 'nvidia',
          total_gpus: 2,
          arch_summary: {},
          unit_plans: [],
          replica_plans: [
            {
              seq: 1,
              node_ids: ['n1'],
              arch: 'nvidia',
              units: [{ role: 'full', gpu_uuids: ['gpu-1'], fit: true, headroom_gb: 1 }],
              fit: true,
              issues: [],
            },
            {
              seq: 2,
              node_ids: ['n2'],
              arch: 'nvidia',
              units: [{ role: 'full', gpu_uuids: ['gpu-3'], fit: true, headroom_gb: 1 }],
              fit: true,
              issues: [],
            },
          ],
        },
      }),
    )
    // 模拟进入本步时的推荐预选:份 1 已有落点
    store.replicaSelections.value = [['gpu-1']]
    const wrapper = mountStep()
    await nextTick()
    expect(inspectDeployment).not.toHaveBeenCalled()

    addReplicaBtn()!.click()
    await nextTick()
    expect(store.form.replicas).toBe(2)
    // 防抖重检请求带新份数
    await vi.waitFor(() => expect(inspectDeployment).toHaveBeenCalledTimes(1))
    expect(vi.mocked(inspectDeployment).mock.calls[0][0].topology).toMatchObject({ replicas: 2 })
    // 重检响应的逐份规划把空份 2 精确回填(n2 的 gpu-3);份 1 手选保持不动
    await vi.waitFor(() =>
      expect(store.replicaSelections.value).toEqual([['gpu-1'], ['gpu-3']]),
    )
    // 各份等量合法:可推进
    expect(store.derivedTopology.value).toMatchObject({ tp: 1, replicas: 2 })
    wrapper.unmount()
  })

  it('多副本全选升级为自动分配:按桶贪心配对各份等量', async () => {
    const wrapper = mountStep()
    await nextTick()
    addReplicaBtn()!.click()
    await nextTick()
    // 4 张可选卡(n1×3 + n2×1),2 份:每份 2 张;桶大优先 → n1 桶出 2 份各 2 张?不足,
    // 实际 n1 只有 3 张:份 1 取 2,份 2 无满桶 → 空份,余 1 张并入激活份由校验提示
    const autoBtn = [...document.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('自动分配'),
    )
    expect(autoBtn).toBeTruthy()
    autoBtn!.click()
    await nextTick()
    expect(store.replicaSelections.value.length).toBe(2)
    expect(store.replicaSelections.value[0].length).toBe(2)
    // n2 的单卡与 n1 的 1 张余数并入激活份(默认激活份 0?此处添加后激活 1)→ 规则提示卡数不一致
    const vm = wrapper.vm as unknown as { canNext: boolean }
    expect(vm.canNext).toBe(false)
    wrapper.unmount()
  })

  it('改最大上下文:防抖重检并携带 max_model_len,选「自动」后不携带(留空按推荐)', async () => {
    store.form.modelId = 7
    store.form.clusterId = 1
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
    const wrapper = mountStep()
    await nextTick()

    maxLenInput(wrapper).vm.$emit('update:value', 4096)
    await vi.waitFor(() => expect(inspectDeployment).toHaveBeenCalledTimes(1))
    expect(vi.mocked(inspectDeployment).mock.calls[0][0].topology).toEqual({
      replicas: 1,
      max_model_len: 4096,
    })

    // 回退自动:下拉选「自动」项(输入框已无 clear 按钮,这是唯一回推荐态的入口)
    maxLenSuffixTrigger().click()
    await nextTick()
    await nextTick()
    ;[...document.querySelectorAll('.n-dropdown-option')]
      .find((el) => el.textContent?.trim() === '自动(留空按推荐)')!
      .querySelector('.n-dropdown-option-body')!
      .click()
    expect(store.form.maxModelLen).toBeNull()
    await vi.waitFor(() => expect(inspectDeployment).toHaveBeenCalledTimes(2))
    expect(vi.mocked(inspectDeployment).mock.calls[1][0].topology).toEqual({ replicas: 1 })
    wrapper.unmount()
  })

  it('最大上下文快捷档下拉(输入框 suffix):选中写入 token 数并触发重检,当前档位带 ✓', async () => {
    store.form.modelId = 7
    store.form.clusterId = 1
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
    const wrapper = mountStep()
    await nextTick()

    // 触发器为输入框 suffix 内的箭头图标(与输入框合成一个组合框,无独立按钮)
    const suffixTrigger = maxLenSuffixTrigger
    expect(suffixTrigger()).toBeTruthy()
    const optionTexts = () =>
      [...document.querySelectorAll('.n-dropdown-option')].map((el) => el.textContent?.trim())

    suffixTrigger()!.click()
    await nextTick()
    await nextTick()
    // 初始默认 8K:8K 档位带 ✓,「自动」项无 ✓
    expect(optionTexts()).toContain('✓ 8K(8192)')
    expect(optionTexts()).not.toContain('✓ 自动(留空按推荐)')
    expect(optionTexts()).toContain('32K(32768)')
    expect(optionTexts().some((text) => text?.startsWith('1M('))).toBe(true)

    // 选中 32K:写入 32768 走同一条防抖重检链路
    // (naive-ui 的 onClick 挂在 option-body 子节点上,须点该节点,点根节点不会触发)
    ;[...document.querySelectorAll('.n-dropdown-option')]
      .find((el) => el.textContent?.trim() === '32K(32768)')!
      .querySelector('.n-dropdown-option-body')!
      .click()
    await nextTick()
    expect(store.form.maxModelLen).toBe(32 * 1024)
    await vi.waitFor(() => expect(inspectDeployment).toHaveBeenCalledTimes(1))
    expect(vi.mocked(inspectDeployment).mock.calls[0][0].topology).toMatchObject({
      max_model_len: 32 * 1024,
    })

    // 重开菜单:当前值命中的档位带 ✓ 标记(自动项 ✓ 消失);改为非档位值后 ✓ 实时消失
    suffixTrigger()!.click()
    await nextTick()
    await nextTick()
    expect(optionTexts()).toContain('✓ 32K(32768)')
    expect(optionTexts()).not.toContain('✓ 自动(留空按推荐)')
    maxLenInput(wrapper).vm.$emit('update:value', 40960)
    await nextTick()
    expect(optionTexts().some((text) => text?.includes('✓'))).toBe(false)
    wrapper.unmount()
  })
})

describe('StepGpuSelect 高级参数(KV Cache 精度)', () => {
  /** 后端 adapter 声明的选项表(此处只取 vLLM 两档:默认 + fp8) */
  const kvOptions = [
    {
      value: '',
      label: '默认(不量化)',
      dtype_bytes: 2,
      kv_ratio: 1,
      args: [],
      hint: 'KV 按模型原始精度存储',
    },
    {
      value: 'fp8_e4m3',
      label: 'FP8 E4M3',
      dtype_bytes: 1,
      kv_ratio: 0.5,
      args: ['--kv-cache-dtype', 'fp8_e4m3'],
      hint: 'KV 约减半;需 CUDA 11.8+',
    },
  ]

  /** 引擎能力字典按生效引擎查表:直接写 store.engines + form.engine(auto 会走推荐引擎) */
  function seedEngine(engine: string, options = kvOptions) {
    store.engines.value = [
      {
        engine,
        display_name: engine,
        deployable: true,
        kv_cache_dtype_options: options,
      },
    ] as unknown as typeof store.engines.value
    store.form.engine = engine
  }

  beforeEach(() => {
    store.engines.value = []
    store.modelOptions.value = []
    store.form.engine = 'auto'
  })

  it('引擎未声明 KV 精度选项:不渲染高级参数入口', async () => {
    seedEngine('vllm', [])
    const wrapper = mountStep()
    await nextTick()
    expect(document.querySelector('[aria-label="高级参数"]')).toBeNull()
    wrapper.unmount()
  })

  it('入口默认收起:仅渲染「高级」按钮,浮层内容不挂载', async () => {
    // n-popover(trigger=click) 内容懒挂载,未打开时下拉不在 DOM;打开后的选项文案由下方 store 用例覆盖
    seedEngine('vllm')
    const wrapper = mountStep()
    await nextTick()
    expect(document.querySelector('[aria-label="高级参数"]')).not.toBeNull()
    expect(document.body.textContent).toContain('高级')
    expect(document.querySelector('.n-base-selection')).toBeNull()
    wrapper.unmount()
  })

  it('下拉选项文案:默认档原名,量化档带占用倍率标注', () => {
    seedEngine('vllm')
    expect(store.kvCacheSelectOptions.value).toEqual([
      { label: '默认(不量化)', value: '' },
      { label: 'FP8 E4M3(KV 约 0.5×)', value: 'fp8_e4m3' },
    ])
  })

  it('选中量化档:写入 store 并随防抖重检传 engine_params.extra_args', async () => {
    seedEngine('vllm')
    store.form.modelId = 7
    store.form.clusterId = 1
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
    const wrapper = mountStep()
    await nextTick()

    store.form.kvCacheDtype = 'fp8_e4m3'
    await nextTick()

    await vi.waitFor(() => expect(inspectDeployment).toHaveBeenCalledTimes(1))
    // 单一事实源:选中档的原生 flag 随预检进 extra_args,后端据此折算 KV 项
    expect(vi.mocked(inspectDeployment).mock.calls[0][0].engine_params).toEqual({
      extra_args: ['--kv-cache-dtype', 'fp8_e4m3'],
    })
    wrapper.unmount()
  })

  it('默认档(不量化):重检不携带 engine_params', async () => {
    seedEngine('vllm')
    store.form.modelId = 7
    store.form.clusterId = 1
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
    const wrapper = mountStep()
    await nextTick()

    // 改部署规格(最大上下文输入)触发同一条防抖重检链路
    wrapper.findAllComponents({ name: 'InputNumber' })[0].vm.$emit('update:value', 2)
    await vi.waitFor(() => expect(inspectDeployment).toHaveBeenCalledTimes(1))
    expect(vi.mocked(inspectDeployment).mock.calls[0][0].engine_params).toBeUndefined()
    wrapper.unmount()
  })

  it('embedding 模型(无 KV cache):即使引擎声明了选项也不渲染', async () => {
    seedEngine('vllm')
    store.form.modelId = 7
    store.modelOptions.value = [
      { label: 'e5', value: 7, raw: { model_type: 'embedding' } },
    ] as unknown as typeof store.modelOptions.value
    const wrapper = mountStep()
    await nextTick()
    expect(document.querySelector('[aria-label="高级参数"]')).toBeNull()
    wrapper.unmount()
  })

  it('引擎切换后旧档位不在新引擎词汇里:回退默认档', async () => {
    seedEngine('vllm')
    store.form.kvCacheDtype = 'fp8_e4m3'
    const wrapper = mountStep()
    await nextTick()
    expect(store.kvCacheOption.value?.value).toBe('fp8_e4m3')

    // SGLang 词汇表不含该档时,残留值会让下拉空白且继续下发旧 flag
    seedEngine('sglang', [
      { value: '', label: '默认(不量化)', dtype_bytes: 2, kv_ratio: 1, args: [], hint: '' },
    ])
    await nextTick()
    expect(store.form.kvCacheDtype).toBe('')
    wrapper.unmount()
  })
})
