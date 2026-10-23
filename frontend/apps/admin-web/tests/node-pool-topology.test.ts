// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/topology', () => ({ getAllTopologies: vi.fn() }))

// VueFlow 依赖画布测量/ResizeObserver,测试环境以插槽桩替代(对齐 deploy-detail-view.test.ts):
// 事件订阅收集到共享 handlers,便于用例直接触发节点/画布点击
const flowHandlers = vi.hoisted(
  () =>
    ({}) as {
      nodeClick?: (event: { node: { id: string; type: string } }) => void
      paneClick?: () => void
    },
)
const fitViewSpy = vi.hoisted(() => vi.fn())
const routerPush = vi.hoisted(() => vi.fn())
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
      fitView: fitViewSpy,
      onNodeClick: (fn: (event: { node: { id: string; type: string } }) => void) => {
        flowHandlers.nodeClick = fn
      },
      onPaneClick: (fn: () => void) => {
        flowHandlers.paneClick = fn
      },
      onNodeMouseEnter: () => {},
      onNodeMouseLeave: () => {},
    }),
    useNodesInitialized: () => ref(true),
  }
})
vi.mock('@vue-flow/background', () => ({ Background: { template: '<div />' } }))
vi.mock('@vue-flow/controls', () => ({ Controls: { template: '<div />' } }))
vi.mock('@vue-flow/minimap', () => ({ MiniMap: { template: '<div />' } }))
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: routerPush, replace: vi.fn() }),
}))

import { getAllTopologies, type ClusterTopology } from '@/api/topology'
import TopologyView from '@/views/node-pool/topology/TopologyView.vue'
import { buildGraph } from '@/views/node-pool/topology/graph'
import {
  DEPLOY_STATUS_MAP,
  GPU_STATUS_MAP,
  NODE_STATUS_MAP,
  POLL_INTERVAL_MS,
  useStore,
  type ClusterGraphData,
  type FlowDeploy,
  type FlowGpu,
  type FlowModel,
  type FlowNode,
} from '@/views/node-pool/topology/useStore'

function makeGraphData(overrides: Partial<ClusterGraphData> = {}): ClusterGraphData {
  return {
    cluster: { id: 1, name: 'gpu-shared', type: 'docker', status: 'active' },
    nodes: [
      { id: 'n1', clusterId: 1, hostname: 'gpu-01', ip: '10.0.0.1', status: 'online' } as FlowNode,
      { id: 'n2', clusterId: 1, hostname: 'gpu-02', ip: '10.0.0.2', status: 'offline' } as FlowNode,
    ],
    gpus: [
      { id: 1, uuid: 'g1', nodeId: 'n1', name: 'A100', memory: 80, util: 50, status: 'online' } as FlowGpu,
      { id: 2, uuid: 'g2', nodeId: 'n2', name: 'A100', memory: 80, util: 0, status: 'offline' } as FlowGpu,
    ],
    deploys: [
      {
        id: 'd1',
        clusterId: 1,
        name: 'prod',
        status: 'running',
        gpuUuids: ['g1', 'g-unknown'],
        engineKey: 'vllm',
        modelId: 1,
        replicas: 1,
        endpoint: null,
        healthStatus: 'healthy',
        replicaSummaries: [{ seq: 0, status: 'serving', nodeIds: ['n1'] }],
      } as FlowDeploy,
      {
        id: 'd2',
        clusterId: 1,
        name: 'dev',
        status: 'stopped',
        gpuUuids: ['g2'],
        engineKey: 'sglang',
        modelId: 2,
        replicas: 1,
        endpoint: null,
        healthStatus: null,
        replicaSummaries: [],
      } as FlowDeploy,
    ],
    engines: [
      { key: 'vllm', name: 'vLLM', version: '0.9.5' },
      { key: 'sglang', name: 'SGLang', version: '--' },
    ],
    models: [
      { id: 1, name: 'DeepSeek-V3', category: 'llm', format: 'safetensors', sizeGb: 671 } as FlowModel,
    ],
    ...overrides,
  }
}

describe('topology buildGraph(纯函数)', () => {
  it('compact 模式:节点/GPU 折叠为聚合块,仅 running 部署边带动画', () => {
    const { nodes, edges } = buildGraph(makeGraphData(), 'compact')
    const types = nodes.map((node) => node.type)
    expect(types).toContain('aggregate')
    expect(types).not.toContain('gpu')

    const clusterNode = nodes.find((node) => node.type === 'cluster')!
    expect(clusterNode.data.record).toMatchObject({ nodeCount: 2, gpuTotal: 2 })
    const aggregate = nodes.find((node) => node.type === 'aggregate')!
    expect(aggregate.data.record).toMatchObject({ nodeCount: 2, gpuTotal: 2, onlineNodes: 1 })

    // 聚合块→部署:d1 running 动画,d2 stopped 静态
    const d1Edge = edges.find((edge) => edge.target === 'deploy-d1' && edge.source.startsWith('aggregate'))
    const d2Edge = edges.find((edge) => edge.target === 'deploy-d2' && edge.source.startsWith('aggregate'))
    expect(d1Edge?.animated).toBe(true)
    expect(d2Edge?.animated).toBe(false)
  })

  it('detail 模式:全链展开,gpu→deploy 边仅认已知 uuid', () => {
    const { nodes, edges } = buildGraph(makeGraphData(), 'detail')
    const types = nodes.map((node) => node.type)
    expect(types).toEqual(expect.arrayContaining(['cluster', 'node', 'gpu', 'deploy', 'engine', 'model']))
    expect(nodes.filter((node) => node.type === 'node')).toHaveLength(2)
    expect(nodes.filter((node) => node.type === 'gpu')).toHaveLength(2)

    // 结构边:cluster→node、node→gpu(静态)
    expect(edges.some((edge) => edge.source === 'cluster-1' && edge.target === 'node-n1')).toBe(true)
    expect(edges.some((edge) => edge.source === 'node-n1' && edge.target === 'gpu-g1')).toBe(true)

    // gpu→deploy:g1→d1 running 动画;g-unknown 不产生边;g2→d2 静态
    const gpuDeployEdges = edges.filter((edge) => edge.source.startsWith('gpu-'))
    expect(gpuDeployEdges.some((edge) => edge.source === 'gpu-g1' && edge.target === 'deploy-d1' && edge.animated)).toBe(true)
    expect(gpuDeployEdges.some((edge) => edge.source === 'gpu-g-unknown')).toBe(false)
    expect(gpuDeployEdges.some((edge) => edge.source === 'gpu-g2' && edge.target === 'deploy-d2' && !edge.animated)).toBe(true)
  })

  it('部署记录聚合引擎名/模型名回退;引擎记录带部署计数;健康态入部署负载,副本摘要反查节点承载引用', () => {
    const { nodes } = buildGraph(makeGraphData(), 'detail')
    const d1 = nodes.find((node) => node.id === 'deploy-d1')!
    expect(d1.data.record).toMatchObject({ engineName: 'vLLM 0.9.5', modelName: 'DeepSeek-V3' })
    // 健康态透传到部署卡片负载(详情面板「健康状态」行数据源)
    expect(d1.data.record).toMatchObject({ healthStatus: 'healthy' })

    // sglang version '--' → engineName 去尾 '--';modelId=2 无对应模型 → '--'
    const d2 = nodes.find((node) => node.id === 'deploy-d2')!
    expect(d2.data.record).toMatchObject({ engineName: 'SGLang', modelName: '--' })

    // 节点承载副本引用:d1 的副本摘要(后端 seq 0 基)落点 n1 → 展示 1 基「副本1 · prod」
    const n1 = nodes.find((node) => node.id === 'node-n1')!
    expect(n1.data.record).toMatchObject({ replicaRefs: ['副本1 · prod'] })
    const n2 = nodes.find((node) => node.id === 'node-n2')!
    expect(n2.data.record).toMatchObject({ replicaRefs: [] })

    const engine = nodes.find((node) => node.id === 'engine-vllm')!
    expect(engine.data.record).toMatchObject({ deployCount: 1 })
  })

  it('状态映射常量(节点/GPU/部署)', () => {
    expect(NODE_STATUS_MAP.drained).toEqual({ label: '已排空', dot: 'bg-secondary' })
    expect(GPU_STATUS_MAP.stale.dot).toBe('bg-warning')
    expect(DEPLOY_STATUS_MAP.deploying.dot).toBe('bg-warning')
  })

  it('相同实体不同输入顺序:各节点落位一致(轮询刷新不跳动)', () => {
    const base = makeGraphData()
    const forward = buildGraph(base, 'detail')
    const shuffled = makeGraphData({
      nodes: [...base.nodes].reverse(),
      gpus: [...base.gpus].reverse(),
      deploys: [...base.deploys].reverse(),
      engines: [...base.engines].reverse(),
      models: [...base.models].reverse(),
    })
    const backward = buildGraph(shuffled, 'detail')

    const posOf = (graph: ReturnType<typeof buildGraph>) =>
      new Map(graph.nodes.map((node) => [node.id, node.position]))
    const forwardPos = posOf(forward)
    for (const node of backward.nodes) {
      expect(forwardPos.get(node.id)).toEqual(node.position)
    }
  })
})

describe('topology useStore 数据适配', () => {
  beforeEach(() => vi.clearAllMocks())

  it('fetchTopologies:字段映射 + 引擎/模型仅保留被引用项 + util 四舍五入', async () => {
    vi.mocked(getAllTopologies).mockResolvedValue([
      {
        cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
        nodes: [
          {
            id: 'n1',
            cluster_id: 1,
            hostname: 'h1',
            ip: '10.0.0.1',
            status: 'online',
            memory_gb: 64,
            available_memory_gb: 48,
            memory_used_gb: 16,
            memory_percent: 25,
            cpu_percent: 12.3,
          },
        ],
        gpus: [
          {
            id: 1,
            uuid: 'g1',
            node_id: 'n1',
            name: 'A100',
            total_vram_gb: 80,
            used_vram_gb: 40.2,
            available_vram_gb: 39.8,
            util: 55.6,
            status: 'online',
          },
        ],
        deployments: [
          {
            deployment_id: 'd1',
            cluster_id: 1,
            name: 'prod',
            status: 'running',
            gpu_uuids: ['g1'],
            engine: 'vllm',
            model_id: 1,
            replicas: 1,
            api_endpoint: 'http://x',
          },
        ],
        engines: [
          { engine: 'vllm', display_name: 'vLLM', image: 'reg/vllm:0.9.5' },
          { engine: 'unused', display_name: 'Unused', image: 'reg/unused:latest' },
        ],
        models: [
          { id: 1, name: 'DeepSeek-V3', model_type: 'llm', format: 'safetensors', size_gb: 671 },
          { id: 2, name: 'UnusedModel', model_type: 'llm', format: null, size_gb: null },
        ],
      } as unknown as ClusterTopology,
    ])

    const store = useStore()
    const [graph] = await store.fetchTopologies()
    expect(graph.deploys[0]).toMatchObject({
      id: 'd1',
      gpuUuids: ['g1'],
      engineKey: 'vllm',
      endpoint: 'http://x',
    })
    expect(graph.gpus[0].util).toBe(56) // Math.round
    // 节点 CPU/内存 + GPU 已用/可用显存映射(snake_case → camelCase)
    expect(graph.nodes[0]).toMatchObject({
      memoryGb: 64,
      memoryUsedGb: 16,
      memoryPercent: 25,
      cpuPercent: 12.3,
    })
    expect(graph.gpus[0]).toMatchObject({ usedVramGb: 40.2, availVramGb: 39.8 })
    expect(graph.engines).toHaveLength(1)
    expect(graph.engines[0]).toMatchObject({ key: 'vllm', version: '0.9.5' })
    expect(graph.models).toHaveLength(1)
    expect(graph.models[0]).toMatchObject({ sizeGb: 671 })
  })

  it('引擎版本解析:registry 带端口无 tag 归 --,空 tag 归 --', async () => {
    vi.mocked(getAllTopologies).mockResolvedValue([
      {
        cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
        nodes: [],
        gpus: [],
        deployments: [
          { deployment_id: 'd1', cluster_id: 1, name: 'p', status: 'running', gpu_uuids: [], engine: 'a', model_id: 1, replicas: 1, api_endpoint: null },
          { deployment_id: 'd2', cluster_id: 1, name: 'q', status: 'running', gpu_uuids: [], engine: 'b', model_id: 1, replicas: 1, api_endpoint: null },
        ],
        engines: [
          { engine: 'a', display_name: 'A', image: 'registry.local:5000/vllm' }, // 带端口无 tag
          { engine: 'b', display_name: 'B', image: 'vllm:' }, // 空 tag
        ],
        models: [{ id: 1, name: 'M', model_type: 'llm', format: null, size_gb: null }],
      } as unknown as ClusterTopology,
    ])

    const [graph] = await useStore().fetchTopologies()
    expect(graph.engines.find((item) => item.key === 'a')?.version).toBe('--')
    expect(graph.engines.find((item) => item.key === 'b')?.version).toBe('--')
  })
})

describe('topology useController 轮询刷新', () => {
  /** 单集群拓扑 mock:一个在线节点(hostname/cpuPercent 驱动面板与画布断言) */
  function makeTopologyList(
    clusterId: number,
    node: { hostname: string; cpuPercent: number } | null = null,
  ): ClusterTopology[] {
    return [
      {
        cluster: { id: clusterId, name: `cluster-${clusterId}`, type: 'docker', status: 'active' },
        nodes: node
          ? [
              {
                id: `${clusterId}-n1`,
                cluster_id: clusterId,
                hostname: node.hostname,
                ip: '10.0.0.1',
                status: 'online',
                memory_gb: 64,
                available_memory_gb: 32,
                memory_used_gb: 32,
                memory_percent: 50,
                cpu_percent: node.cpuPercent,
              },
            ]
          : [],
        gpus: [],
        deployments: [],
        engines: [],
        models: [],
      } as unknown as ClusterTopology,
    ]
  }

  function mountView(props: { clusterId?: number } = {}) {
    return mount(TopologyView, { props, attachTo: document.body })
  }

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getAllTopologies).mockResolvedValue(
      makeTopologyList(1, { hostname: 'gpu-01', cpuPercent: 12 }),
    )
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('首拉后每 POLL_INTERVAL_MS 刷一轮;卸载后停止轮询', async () => {
    expect(POLL_INTERVAL_MS).toBe(10_000)
    vi.useFakeTimers()
    try {
      const wrapper = mountView({ clusterId: 1 })
      await vi.advanceTimersByTimeAsync(0)
      expect(getAllTopologies).toHaveBeenCalledTimes(1)

      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
      expect(getAllTopologies).toHaveBeenCalledTimes(2)

      wrapper.unmount()
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2)
      expect(getAllTopologies).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('轮询失败保留旧图,链条继续', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountView({ clusterId: 1 })
      await vi.advanceTimersByTimeAsync(0)
      await vi.waitFor(() => expect(document.body.textContent).toContain('gpu-01'))

      // 下一轮失败:画布保留旧数据,不回到空态
      vi.mocked(getAllTopologies).mockRejectedValueOnce(new Error('network down'))
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
      expect(document.body.textContent).toContain('gpu-01')

      // 链条继续:失败后的下一轮仍正常发起并成功
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
      expect(getAllTopologies).toHaveBeenCalledTimes(3)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('轮询返回空列表:视为瞬态异常,保留旧图不清空画布', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountView({ clusterId: 1 })
      await vi.advanceTimersByTimeAsync(0)
      await vi.waitFor(() => expect(document.body.textContent).toContain('gpu-01'))
      const fitCountAfterMount = fitViewSpy.mock.calls.length

      // 下一轮返回空列表:画布保留旧数据,不回退集群/不清空/不重置视角
      vi.mocked(getAllTopologies).mockResolvedValueOnce([])
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
      await nextTick()
      expect(document.body.textContent).toContain('gpu-01')
      expect(document.body.textContent).not.toContain('暂无集群拓扑数据')
      expect(fitViewSpy.mock.calls.length).toBe(fitCountAfterMount)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('普通轮询(数据小幅变化)不重置视角:fitView 不再触发', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountView({ clusterId: 1 })
      await vi.advanceTimersByTimeAsync(0)
      await vi.waitFor(() => expect(document.body.textContent).toContain('gpu-01'))
      const fitCountAfterMount = fitViewSpy.mock.calls.length
      expect(fitCountAfterMount).toBeGreaterThanOrEqual(1)

      // 数轮轮询:实体集合不变,只有实时值变化 → 不触发 fitView(视角/缩放保持用户状态)
      for (let round = 0; round < 3; round += 1) {
        vi.mocked(getAllTopologies).mockResolvedValueOnce(
          makeTopologyList(1, { hostname: 'gpu-01', cpuPercent: 20 + round }),
        )
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
      }
      await vi.waitFor(() => expect(document.body.textContent).toContain('22%'))
      expect(fitViewSpy.mock.calls.length).toBe(fitCountAfterMount)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('详情面板随轮询自动刷新;选中节点被移除后自动收起', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountView({ clusterId: 1 })
      await vi.advanceTimersByTimeAsync(0)
      flowHandlers.nodeClick?.({ node: { id: 'node-1-n1', type: 'node' } })
      await nextTick()
      expect(document.body.textContent).toContain('节点详情')
      expect(document.body.textContent).toContain('12%')

      // 下一轮 CPU 升高 → 画布与面板同步(record 实时取,非选中时快照)
      vi.mocked(getAllTopologies).mockResolvedValue(
        makeTopologyList(1, { hostname: 'gpu-01', cpuPercent: 77 }),
      )
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
      await vi.waitFor(() => expect(document.body.textContent).toContain('77%'))

      // 节点被移除 → 面板自动收起
      vi.mocked(getAllTopologies).mockResolvedValue(makeTopologyList(1))
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
      await vi.waitFor(() => expect(document.body.textContent).not.toContain('节点详情'))
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('当前集群被移除:轮询回退首个可用集群', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountView({ clusterId: 1 })
      await vi.advanceTimersByTimeAsync(0)
      await vi.waitFor(() => expect(document.body.textContent).toContain('gpu-01'))

      // 下一轮只剩集群 2:自动切换,画布渲染新集群节点
      vi.mocked(getAllTopologies).mockResolvedValue(
        makeTopologyList(2, { hostname: 'gpu-02', cpuPercent: 10 }),
      )
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
      await vi.waitFor(() => expect(document.body.textContent).toContain('gpu-02'))
      expect(document.body.textContent).not.toContain('gpu-01')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('GPU 详情「前往管理页」跳节点列表(GPU 页已下线,GPU 信息并入节点列表)', async () => {
    // makeTopologyList 不含 GPU,这里单独 mock 一份带单卡拓扑
    vi.mocked(getAllTopologies).mockResolvedValue([
      {
        cluster: { id: 1, name: 'cluster-1', type: 'docker', status: 'active' },
        nodes: [
          {
            id: '1-n1',
            cluster_id: 1,
            hostname: 'gpu-01',
            ip: '10.0.0.1',
            status: 'online',
            memory_gb: 64,
            available_memory_gb: 32,
            memory_used_gb: 32,
            memory_percent: 50,
            cpu_percent: 12,
          },
        ],
        gpus: [
          {
            id: 7,
            uuid: 'gpu-uuid-7',
            node_id: '1-n1',
            index: 0,
            name: 'A100',
            arch: 'ampere',
            total_vram_gb: 80,
            used_vram_gb: 10,
            available_vram_gb: 70,
            util: 12,
            status: 'online',
          },
        ],
        deployments: [],
        engines: [],
        models: [],
      } as unknown as ClusterTopology,
    ])
    const wrapper = mountView({ clusterId: 1 })
    await vi.waitFor(() => expect(document.body.textContent).toContain('gpu-01'))

    // 点开 GPU 节点详情,面板出现后点击「前往管理页」(图节点 id 规则:gpu-${uuid})
    flowHandlers.nodeClick?.({ node: { id: 'gpu-gpu-uuid-7', type: 'gpu' } })
    await nextTick()
    expect(document.body.textContent).toContain('GPU 详情')
    const jumpButton = wrapper.findAll('button').find((button) => button.text() === '前往管理页')
    expect(jumpButton).toBeTruthy()
    await jumpButton!.trigger('click')
    expect(routerPush).toHaveBeenCalledWith({ name: 'node-list' })
    wrapper.unmount()
  })
})
