import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h } from 'vue'

vi.mock('@/api/cluster', () => ({ listClusters: vi.fn() }))
vi.mock('@/api/deployment', () => ({ inspectDeployment: vi.fn() }))
vi.mock('@/api/inference-image', () => ({
  listInferenceEngines: vi.fn(),
  listInferenceImages: vi.fn(),
}))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@/api/topology', () => ({ getClusterTopology: vi.fn() }))
// 父级列表 store:提交创建只断言透传的 payload,不引入真实实现
vi.mock('@/views/deploy/components/deploy-list/useStore', () => ({ useStore: vi.fn() }))

import { listInferenceImages } from '@/api/inference-image'
import { inspectDeployment } from '@/api/deployment'
import { useStore as useDeployListStore } from '@/views/deploy/components/deploy-list/useStore'
import type {
  ClusterTopology,
  TopologyGpu,
  TopologyNode,
} from '@/api/topology'
import type { EngineOut, ImageEntryOut } from '@/api/inference-image'
import type { InspectClusterGpu, InspectDeploymentResult, InspectRecommendedPlacement } from '@/api/deployment'
import {
  argLineToTokens,
  buildPublishSpec,
  isProtectedArgLine,
  paramSuggestionOptions,
  toArgLines,
  useWizardStore,
  type WizardPublishValues,
} from '@/views/deploy/components/deploy-list/create-wizard/useStore'

// ---------- 测试夹具 ----------

function makeNode(id: string, status = 'online'): TopologyNode {
  return { id, cluster_id: 1, hostname: id, ip: '127.0.0.1', status } as TopologyNode
}

function makeGpu(
  uuid: string,
  nodeId: string,
  overrides: Partial<TopologyGpu> = {},
): TopologyGpu {
  return {
    id: 1,
    uuid,
    node_id: nodeId,
    index: 0,
    name: 'GPU',
    arch: 'nvidia',
    total_vram_gb: 80,
    available_vram_gb: 80,
    util: 0,
    status: 'online',
    ...overrides,
  } as TopologyGpu
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
    name: 'GPU',
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

function makeInspect(
  overrides: Partial<InspectDeploymentResult> = {},
): InspectDeploymentResult {
  return {
    deployable: true,
    issues: [],
    recommended_cluster_id: 1,
    cross_node: false,
    recommendation: {
      engine: 'vllm',
      image: 'reg/vllm:latest',
      topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 },
      vram_per_gpu_gb: 40,
    },
    cluster_gpus: [],
    node_support: [],
    recommended_placement: null,
    resolved: null,
    ...overrides,
  } as InspectDeploymentResult
}

function makeTopology(overrides: Partial<ClusterTopology> = {}): ClusterTopology {
  return {
    cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
    nodes: [makeNode('n1')],
    gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1')],
    deployments: [],
    ...overrides,
  } as ClusterTopology
}

function makeImageEntry(overrides: Partial<ImageEntryOut> = {}): ImageEntryOut {
  return {
    id: 'img-1',
    engine: 'vllm',
    arch: 'nvidia',
    gpu_model: ['*'],
    driver: null,
    engine_version: '0.9.5',
    image: 'reg/vllm:0.9.5',
    default: true,
    notes: '',
    ...overrides,
  } as ImageEntryOut
}

function makeEngine(overrides: Partial<EngineOut> = {}): EngineOut {
  return {
    engine: 'vllm',
    display_name: 'vLLM',
    supported_formats: ['safetensors'],
    supported_gpu_archs: ['nvidia'],
    supported_categories: ['llm'],
    supported_parallel: ['tp'],
    deployable: true,
    protected_flags: [],
    param_suggestions: [],
    kv_cache_dtype_options: [],
    ...overrides,
  } as EngineOut
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const createDeploy = vi.fn()
// useWizardStore() 在模块顶层调用并捕获 deployStore,mock 返回值必须先就绪
vi.mocked(useDeployListStore).mockReturnValue({
  createDeploy,
} as unknown as ReturnType<typeof useDeployListStore>)
const store = useWizardStore()

beforeEach(() => {
  vi.clearAllMocks()
  store.reset()
})

// ---------- 镜像目录(f8cac24) ----------

describe('loadImageVersions 镜像目录加载', () => {
  it('首次按引擎拉取并写入目录,同引擎重复调用命中缓存不再请求', async () => {
    const entries = [makeImageEntry({ engine_version: '0.9.5' })]
    vi.mocked(listInferenceImages).mockResolvedValue(entries)

    await store.loadImageVersions('vllm')
    expect(listInferenceImages).toHaveBeenCalledTimes(1)
    expect(listInferenceImages).toHaveBeenCalledWith({ engine: 'vllm' })
    expect(store.imageEntries.value).toEqual(entries)
    expect(store.imageEntriesLoading.value).toBe(false)

    await store.loadImageVersions('vllm')
    expect(listInferenceImages).toHaveBeenCalledTimes(1)
  })

  it('并发切换引擎只认最后一次响应,慢的旧响应被丢弃', async () => {
    const slow = deferred<ImageEntryOut[]>()
    const fastEntries = [makeImageEntry({ engine: 'sglang', engine_version: '0.4' })]
    vi.mocked(listInferenceImages)
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(fastEntries)

    const first = store.loadImageVersions('vllm')
    const second = store.loadImageVersions('sglang')
    await second
    expect(store.imageEntries.value).toEqual(fastEntries)
    expect(store.imageEntriesLoading.value).toBe(false)

    // 迟到的 vllm 响应不得覆盖 sglang 结果
    slow.resolve([makeImageEntry({ engine: 'vllm', engine_version: '0.9' })])
    await first
    expect(store.imageEntries.value).toEqual(fastEntries)
    expect(store.imageEntriesLoading.value).toBe(false)
  })

  it('加载失败清空目录并置空缓存键,下次同引擎可重试', async () => {
    vi.mocked(listInferenceImages)
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce([makeImageEntry()])

    await store.loadImageVersions('vllm')
    expect(store.imageEntries.value).toEqual([])
    expect(store.imageEntriesLoading.value).toBe(false)

    // 缓存键已置空:同引擎再次触发会重新请求而非跳过
    await store.loadImageVersions('vllm')
    expect(listInferenceImages).toHaveBeenCalledTimes(2)
    expect(store.imageEntries.value).toHaveLength(1)
  })
})

// ---------- 引擎字典 ----------

describe('引擎字典展示与下拉过滤', () => {
  it('engineLabel:auto 显示自动选择,已知引擎取 display_name,未知引擎回退原值', () => {
    store.engines.value = [
      makeEngine({ engine: 'vllm', display_name: 'vLLM' }),
      makeEngine({ engine: 'sglang', display_name: 'SGLang' }),
    ]
    expect(store.engineLabel('auto')).toBe('自动选择')
    expect(store.engineLabel('vllm')).toBe('vLLM')
    expect(store.engineLabel('llamacpp')).toBe('llamacpp')
  })

  it('engineOptionsFor:首项为自动选择,且仅包含 deployable 引擎', () => {
    store.engines.value = [
      makeEngine({ engine: 'vllm', display_name: 'vLLM' }),
      makeEngine({ engine: 'sglang', display_name: 'SGLang' }),
      makeEngine({ engine: 'llamacpp', display_name: 'llama.cpp', deployable: false }),
    ]
    const options = store.engineOptionsFor()
    expect(options).toEqual([
      { label: '自动选择', value: 'auto' },
      { label: 'vLLM', value: 'vllm' },
      { label: 'SGLang', value: 'sglang' },
    ])
  })

  it('engineOptionsFor:后端判定的不支持项置灰并带原因后缀', () => {
    store.engines.value = [
      makeEngine({ engine: 'vllm', display_name: 'vLLM', supports_model: true }),
      makeEngine({
        engine: 'llamacpp',
        display_name: 'llama.cpp',
        supports_model: false,
        unsupported_reason: '格式 safetensors 不支持(支持 gguf)',
      }),
    ]
    expect(store.engineOptionsFor()).toEqual([
      { label: '自动选择', value: 'auto' },
      { label: 'vLLM', value: 'vllm' },
      {
        label: 'llama.cpp（格式 safetensors 不支持(支持 gguf)）',
        value: 'llamacpp',
        disabled: true,
      },
    ])
  })

  it('loadEngines:按模型缓存,模型变化重拉', async () => {
    const listInferenceEngines = vi.mocked(
      (await import('@/api/inference-image')).listInferenceEngines,
    )
    listInferenceEngines.mockResolvedValue([makeEngine({ engine: 'vllm' })])
    store.engines.value = []
    await store.loadEngines(7)
    expect(listInferenceEngines).toHaveBeenCalledWith(7)
    await store.loadEngines(7) // 命中缓存
    expect(listInferenceEngines).toHaveBeenCalledTimes(1)
    await store.loadEngines(8) // 模型变了重拉(后端按模型判定)
    expect(listInferenceEngines).toHaveBeenCalledTimes(2)
    expect(listInferenceEngines).toHaveBeenLastCalledWith(8)
  })
})

// ---------- 拓扑推导与跨机校验 ----------

describe('derivedTopology 按选卡自动推导', () => {
  it('未选卡时为 null', () => {
    expect(store.derivedTopology.value).toBeNull()
  })

  it('跨节点选卡且引擎支持多机 PP(宽松兜底):节点内 TP + 跨机 PP', () => {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1'), makeGpu('gpu-3', 'n2'), makeGpu('gpu-4', 'n2')],
    })
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-3'])
    expect(store.derivedTopology.value).toEqual({
      tp: 1,
      pp: 2,
      replicas: 1,
      nnodes: 2,
      max_model_len: 8 * 1024,
    })
  })

  it('跨节点选卡但词表无 pp(如 SGLang 0.4.2):维持旧形态跨机 TP', () => {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1'), makeGpu('gpu-3', 'n2'), makeGpu('gpu-4', 'n2')],
    })
    store.engines.value = [makeEngine({ supported_parallel: ['tp'] })]
    store.form.engine = 'vllm'
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-3'])
    expect(store.derivedTopology.value).toEqual({
      tp: 2,
      pp: 1,
      replicas: 1,
      nnodes: 2,
      max_model_len: 8 * 1024,
    })
  })

  it('跨节点选卡且引擎无多机(如旧 vLLM 镜像):blocked,拓扑回落跨机 TP 形态(选卡由收敛机制拦单机)', () => {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1'), makeGpu('gpu-3', 'n2'), makeGpu('gpu-4', 'n2')],
    })
    store.engines.value = [makeEngine({ supports_multinode: false })]
    store.form.engine = 'vllm'
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-3'])
    // 瞬态(收敛前):仍给合法可提交的旧形态,由 topologyRuleError 提示收敛
    expect(store.derivedTopology.value).toMatchObject({ tp: 2, pp: 1, nnodes: 2 })
    expect(store.crossNodePpBlocked.value).toBe(true)
    expect(store.topologyRuleError.value).toContain('不支持多机部署')
  })

  it('inspect 推荐优先于引擎字典:recommendation.supports_multinode=false 时 blocked', () => {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1'), makeGpu('gpu-3', 'n2'), makeGpu('gpu-4', 'n2')],
    })
    // 字典说支持多机(引擎级),inspect 按镜像版本折算说不支持(版本感知,最准)
    store.engines.value = [makeEngine({ supports_multinode: true })]
    store.form.engine = 'vllm'
    store.inspect.value = makeInspect({
      recommendation: {
        engine: 'vllm',
        image: 'reg/vllm:0.7.3',
        topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 },
        vram_per_gpu_gb: 40,
        supports_multinode: false,
      },
    })
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-3'])
    expect(store.engineSupportsMultinode.value).toBe(false)
    expect(store.crossNodePpBlocked.value).toBe(true)
  })

  it('cann 选卡:即使引擎支持多机也 blocked(昇腾不支持多机)', () => {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('gpu-1', 'n1', { arch: 'cann' }), makeGpu('gpu-2', 'n1', { arch: 'cann' }), makeGpu('gpu-3', 'n2', { arch: 'cann' }), makeGpu('gpu-4', 'n2', { arch: 'cann' })],
    })
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-3'])
    expect(store.selectionIsCann.value).toBe(true)
    expect(store.crossNodePpBlocked.value).toBe(true)
    expect(store.derivedTopology.value).toMatchObject({ tp: 2, pp: 1, nnodes: 2 })
  })

  it('多副本跨节点选卡不适用 PP 分流:每份单节点,nnodes 恒 1', () => {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1'), makeGpu('gpu-3', 'n2'), makeGpu('gpu-4', 'n2')],
    })
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-2'])
    store.addReplica()
    store.selectedGpuUuids.value = new Set(['gpu-3', 'gpu-4'])
    expect(store.derivedTopology.value).toMatchObject({ tp: 2, pp: 1, replicas: 2, nnodes: 1 })
  })

  it('max_model_len 用户值优先,否则取 inspect 推荐值', () => {
    store.topology.value = makeTopology({ gpus: [makeGpu('gpu-1', 'n1')] })
    store.selectedGpuUuids.value = new Set(['gpu-1'])

    store.inspect.value = makeInspect()
    // 默认 8K 算用户值;置空(null)才回退 inspect 推荐值
    store.form.maxModelLen = null
    expect(store.derivedTopology.value?.max_model_len).toBe(8192)

    store.form.maxModelLen = 4096
    expect(store.derivedTopology.value?.max_model_len).toBe(4096)
  })
})

describe('topologyRuleError 跨机约束校验', () => {
  function selectSpread(counts: Record<string, number>) {
    const gpus: TopologyGpu[] = []
    let seq = 0
    for (const [nodeId, count] of Object.entries(counts)) {
      for (let i = 0; i < count; i += 1) {
        seq += 1
        gpus.push(makeGpu(`gpu-${nodeId}-${i}`, nodeId, { id: seq }))
      }
    }
    const nodeIds = Object.keys(counts)
    store.topology.value = makeTopology({
      nodes: nodeIds.map((id) => makeNode(id)),
      gpus,
    })
    store.selectedGpuUuids.value = new Set(gpus.map((gpu) => gpu.uuid))
  }

  it('单节点部署不触发跨机校验', () => {
    selectSpread({ n1: 2 })
    expect(store.topologyRuleError.value).toBeNull()
  })

  it('跨节点卡数不整除节点数时报错', () => {
    selectSpread({ n1: 2, n2: 1 })
    expect(store.topologyRuleError.value).toMatch(/整除/)
    expect(store.topologyRuleError.value).toContain('3 张卡分布在 2 个节点')
  })

  it('跨节点整除但各节点卡数不一致时报错', () => {
    selectSpread({ n1: 3, n2: 1 })
    expect(store.topologyRuleError.value).toMatch(/各节点卡数一致/)
    expect(store.topologyRuleError.value).toContain('每节点 2 张')
  })

  it('跨节点均分(2+2)时校验通过', () => {
    selectSpread({ n1: 2, n2: 2 })
    expect(store.topologyRuleError.value).toBeNull()
  })
})

// ---------- 选卡 ----------

describe('GPU 可选性与禁选原因', () => {
  /**
   * 拓扑:n1 在线,n2 离线
   *   gpu-ok      n1 在线卡,评估正常
   *   gpu-node-off n2 卡(节点离线)
   *   gpu-off      n1 卡但 GPU 离线
   *   gpu-busy     n1 在线卡但已被部署占用
   *   gpu-arch     n1 在线卡,评估 arch_mismatch
   *   gpu-inspect-off n1 在线卡,评估 offline
   *   gpu-group-b  n1 在线卡,评估正常但分组为 g-b(与 gpu-ok 的 g-a 不同)
   *   gpu-no-image n1 在线卡,评估 no_image(带 grey_detail)
   */
  function setupTopology() {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2', 'offline')],
      gpus: [
        makeGpu('gpu-ok', 'n1'),
        makeGpu('gpu-node-off', 'n2'),
        makeGpu('gpu-off', 'n1', { status: 'offline' }),
        makeGpu('gpu-busy', 'n1'),
        makeGpu('gpu-arch', 'n1'),
        makeGpu('gpu-inspect-off', 'n1'),
        makeGpu('gpu-group-b', 'n1'),
        makeGpu('gpu-no-image', 'n1'),
      ],
      deployments: [
        {
          deployment_id: 'd1',
          cluster_id: 1,
          name: 'dep',
          status: 'running',
          phase: 'RUNNING',
          gpu_uuids: ['gpu-busy'],
        } as ClusterTopology['deployments'][number],
      ],
    })
    store.inspect.value = makeInspect({
      cluster_gpus: [
        makeAssessed('gpu-ok', 'n1'),
        makeAssessed('gpu-arch', 'n1', { grey_reason: 'arch_mismatch' }),
        makeAssessed('gpu-inspect-off', 'n1', { grey_reason: 'offline' }),
        makeAssessed('gpu-group-b', 'n1', { group_key: 'g-b' }),
        makeAssessed('gpu-no-image', 'n1', {
          grey_reason: 'no_image',
          grey_detail: 'vLLM 对 A100(驱动 570)无匹配镜像',
        }),
      ],
    })
  }

  function gpuOf(uuid: string): TopologyGpu {
    const gpu = store.topology.value?.gpus.find((item) => item.uuid === uuid)
    if (!gpu) throw new Error(`missing fixture gpu: ${uuid}`)
    return gpu
  }

  it('节点离线/GPU 离线/被部署占用的卡均不可选', () => {
    setupTopology()
    expect(store.isGpuSelectable(gpuOf('gpu-ok'))).toBe(true)
    expect(store.isGpuSelectable(gpuOf('gpu-node-off'))).toBe(false)
    expect(store.isGpuSelectable(gpuOf('gpu-off'))).toBe(false)
    expect(store.isGpuSelectable(gpuOf('gpu-busy'))).toBe(false)
  })

  it('inspect 灰化(arch_mismatch/offline)不可选,禁选原因有对应文案', () => {
    setupTopology()
    expect(store.isGpuSelectable(gpuOf('gpu-arch'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('gpu-arch'))).toBe('架构不匹配')
    expect(store.isGpuSelectable(gpuOf('gpu-inspect-off'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('gpu-inspect-off'))).toBe('离线')
  })

  it('no_image 灰化不可选,禁选原因优先取 grey_detail 人读详情', () => {
    setupTopology()
    expect(store.isGpuSelectable(gpuOf('gpu-no-image'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('gpu-no-image'))).toBe(
      'vLLM 对 A100(驱动 570)无匹配镜像',
    )
  })

  it('grey_detail 缺失时 no_image 回退兜底文案', () => {
    setupTopology()
    store.inspect.value = makeInspect({
      cluster_gpus: [
        makeAssessed('gpu-ok', 'n1'),
        makeAssessed('gpu-no-image', 'n1', { grey_reason: 'no_image', grey_detail: null }),
      ],
    })
    expect(store.inspectDisabledReason(gpuOf('gpu-no-image'))).toBe('无匹配镜像')
  })

  describe('nodeDisabledReason 节点不可选原因(node_support)', () => {
    function nodeOf(id: string): TopologyNode {
      const node = store.topology.value?.nodes.find((item) => item.id === id)
      if (!node) throw new Error(`missing fixture node: ${id}`)
      return node
    }

    it('selectable=false 返回 reason;reason 缺失回退兜底文案', () => {
      store.topology.value = makeTopology({
        nodes: [makeNode('n1'), makeNode('n2')],
        gpus: [makeGpu('gpu-1', 'n1')],
      })
      store.inspect.value = makeInspect({
        node_support: [
          { node_id: 'n1', hostname: 'n1', selectable: false, reason: '节点无引擎可用 GPU' },
          { node_id: 'n2', hostname: 'n2', selectable: false, reason: null },
        ],
      })
      expect(store.nodeDisabledReason(nodeOf('n1'))).toBe('节点无引擎可用 GPU')
      expect(store.nodeDisabledReason(nodeOf('n2'))).toBe('无引擎可用 GPU')
    })

    it('selectable=true 或未评估(inspect 未加载/无记录)返回 null', () => {
      store.topology.value = makeTopology({
        nodes: [makeNode('n1'), makeNode('n2')],
        gpus: [makeGpu('gpu-1', 'n1')],
      })
      store.inspect.value = makeInspect({
        node_support: [{ node_id: 'n1', hostname: 'n1', selectable: true, reason: null }],
      })
      expect(store.nodeDisabledReason(nodeOf('n1'))).toBeNull()
      // n2 无 node_support 记录
      expect(store.nodeDisabledReason(nodeOf('n2'))).toBeNull()
      // inspect 未加载
      store.inspect.value = null
      expect(store.nodeDisabledReason(nodeOf('n1'))).toBeNull()
    })
  })

  it('已选卡组确定后,其它分组的卡不可选且提示组不一致', () => {
    setupTopology()
    store.selectedGpuUuids.value = new Set(['gpu-ok'])
    expect(store.isGpuSelectable(gpuOf('gpu-group-b'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('gpu-group-b'))).toBe('与已选卡组不一致')
    // 可选卡自身无禁选原因
    expect(store.inspectDisabledReason(gpuOf('gpu-ok'))).toBeNull()
  })

  it('inspect 未评估的卡回退纯拓扑判断(在线且未占用即可选)', () => {
    setupTopology()
    // gpu-busy 无评估数据,但被部署占用 → 仍不可选;占用与评估无关,换一张无评估卡验证可选
    store.topology.value = makeTopology({
      nodes: [makeNode('n1')],
      gpus: [makeGpu('gpu-no-assess', 'n1')],
      deployments: [],
    })
    store.inspect.value = makeInspect({ cluster_gpus: [] })
    expect(store.isGpuSelectable(gpuOf('gpu-no-assess'))).toBe(true)
    expect(store.inspectDisabledReason(gpuOf('gpu-no-assess'))).toBeNull()
  })
})

describe('选卡操作', () => {
  function setupFlat() {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2', 'offline')],
      gpus: [
        makeGpu('a1', 'n1'),
        makeGpu('a2', 'n1'),
        makeGpu('a3', 'n1'),
        makeGpu('b1', 'n2'),
      ],
    })
    store.inspect.value = null
  }

  it('toggleGpu 增删已选,不可选的卡被忽略', () => {
    setupFlat()
    store.toggleGpu(gpu('a1'))
    expect([...store.selectedGpuUuids.value]).toEqual(['a1'])
    store.toggleGpu(gpu('a1'))
    expect(store.selectedGpuUuids.value.size).toBe(0)
    store.toggleGpu(gpu('b1'))
    expect(store.selectedGpuUuids.value.size).toBe(0)
  })

  it('toggleNode 整选/整取消,离线节点忽略', () => {
    setupFlat()
    const n1 = store.topology.value!.nodes[0]
    const n2 = store.topology.value!.nodes[1]
    store.toggleNode(n1)
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a1', 'a2', 'a3'])
    store.toggleNode(n1)
    expect(store.selectedGpuUuids.value.size).toBe(0)
    store.toggleNode(n2)
    expect(store.selectedGpuUuids.value.size).toBe(0)
  })

  it('selectAll 全选可选卡,invertSelection 在可选集合内取反', () => {
    setupFlat()
    store.selectAll()
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a1', 'a2', 'a3'])
    store.toggleGpu(gpu('a2'))
    store.invertSelection()
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a2'])
    store.invertSelection()
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a1', 'a3'])
  })

  it('addGpus 与已选取并集且过滤不可选卡', () => {
    setupFlat()
    store.selectedGpuUuids.value = new Set(['a1'])
    store.addGpus([gpu('a2'), gpu('b1')])
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a1', 'a2'])
  })

  it('removeNodeSelection 移除指定节点下所有已选,clearSelection 清空', () => {
    setupFlat()
    store.selectedGpuUuids.value = new Set(['a1', 'a2'])
    store.removeNodeSelection('n1')
    expect(store.selectedGpuUuids.value.size).toBe(0)

    store.selectedGpuUuids.value = new Set(['a1'])
    store.clearSelection()
    expect(store.selectedGpuUuids.value.size).toBe(0)
  })

  function gpu(uuid: string): TopologyGpu {
    const found = store.topology.value?.gpus.find((item) => item.uuid === uuid)
    if (!found) throw new Error(`missing fixture gpu: ${uuid}`)
    return found
  }
})

describe('llama.cpp 单机约束(仅支持单节点选取)', () => {
  /** 两节点各 2/1 张卡;引擎设为 llamacpp */
  function setupLlama() {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('a1', 'n1'), makeGpu('a2', 'n1'), makeGpu('b1', 'n2')],
    })
    store.inspect.value = null
    store.form.engine = 'llamacpp'
  }

  function gpuOf(uuid: string): TopologyGpu {
    const found = store.topology.value?.gpus.find((item) => item.uuid === uuid)
    if (!found) throw new Error(`missing fixture gpu: ${uuid}`)
    return found
  }

  it('已选某节点卡后,其它节点卡不可选且给出原因', () => {
    setupLlama()
    store.toggleGpu(gpuOf('a1'))
    expect(store.isGpuSelectable(gpuOf('b1'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('b1'))).toBe('llama.cpp 仅支持单节点部署(已选 n1)')
    // 同节点仍可继续选
    expect(store.isGpuSelectable(gpuOf('a2'))).toBe(true)
    // 取消后解锁
    store.toggleGpu(gpuOf('a1'))
    expect(store.isGpuSelectable(gpuOf('b1'))).toBe(true)
  })

  it('节点卡同样按锁定节点禁选(展示同一原因)', () => {
    setupLlama()
    store.toggleGpu(gpuOf('a1'))
    const other = store.topology.value!.nodes.find((node) => node.id === 'n2')!
    expect(store.nodeDisabledReason(other)).toBe('llama.cpp 仅支持单节点部署(已选 n1)')
    expect(store.selectableGpusOf('n2')).toEqual([])
    store.toggleNode(other)
    expect([...store.selectedGpuUuids.value]).toEqual(['a1'])
  })

  it('selectAll / invertSelection 收敛到单节点(候选最多的节点)', () => {
    setupLlama()
    store.selectAll()
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a1', 'a2'])
    store.clearSelection()
    store.invertSelection()
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a1', 'a2'])
  })

  it('框选跨节点只并入锁定节点的卡', () => {
    setupLlama()
    store.toggleGpu(gpuOf('a1'))
    store.addGpus([gpuOf('a2'), gpuOf('b1')])
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a1', 'a2'])
  })

  it('narrowSelectionToSingleNode 收敛残留的跨节点选集', () => {
    setupLlama()
    store.selectedGpuUuids.value = new Set(['a1', 'b1'])
    store.narrowSelectionToSingleNode()
    expect([...store.selectedGpuUuids.value]).toEqual(['a1'])
  })

  it('其它引擎不受单节点约束(vLLM 可跨机 TP)', () => {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('a1', 'n1'), makeGpu('b1', 'n2')],
    })
    store.inspect.value = null
    store.form.engine = 'vllm'
    store.toggleGpu(gpuOf('a1'))
    expect(store.isGpuSelectable(gpuOf('b1'))).toBe(true)
    store.toggleGpu(gpuOf('b1'))
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['a1', 'b1'])
  })
})

describe('CPU 部署(节点卡片点击 = 纯 CPU 部署)', () => {
  /** n1 无引擎可用卡、n2 有可用卡:llama.cpp 下两者都可作 CPU 落点(有卡的机器同样支持纯 CPU) */
  function setupCpuLane() {
    store.engines.value = [
      makeEngine({ engine: 'llamacpp', supported_gpu_archs: ['nvidia', 'cpu'], supported_formats: ['gguf'] }),
    ]
    store.form.engine = 'llamacpp'
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('a1', 'n1', { arch: 'cann' }), makeGpu('b1', 'n2')],
    })
    store.inspect.value = makeInspect({
      node_support: [
        { node_id: 'n1', hostname: 'n1', selectable: false, reason: '节点 GPU 架构不被该引擎支持', memory_gb: 64, available_memory_gb: 60 },
        { node_id: 'n2', hostname: 'n2', selectable: true, reason: null, memory_gb: 64, available_memory_gb: 50 },
      ],
      cluster_gpus: [makeAssessed('a1', 'n1', { grey_reason: 'arch_mismatch' }), makeAssessed('b1', 'n2')],
    })
  }

  function nodeOf(id: string): TopologyNode {
    const found = store.topology.value?.nodes.find((item) => item.id === id)
    if (!found) throw new Error(`missing fixture node: ${id}`)
    return found
  }

  function gpuOf(uuid: string): TopologyGpu {
    const found = store.topology.value?.gpus.find((item) => item.uuid === uuid)
    if (!found) throw new Error(`missing fixture gpu: ${uuid}`)
    return found
  }

  it('所有在线节点都可作 CPU 落点(有无可用卡不影响)', () => {
    setupCpuLane()
    expect(store.engineSupportsCpu.value).toBe(true)
    expect(store.isCpuNodeSelectable(nodeOf('n1'))).toBe(true)
    expect(store.isCpuNodeSelectable(nodeOf('n2'))).toBe(true)
  })

  it('选中 CPU 节点会清空已选 GPU 卡(两种部署互斥)', () => {
    setupCpuLane()
    store.toggleGpu(gpuOf('b1'))
    expect(store.selectedGpuUuids.value.size).toBe(1)

    store.toggleCpuNode(nodeOf('n2'))
    expect(store.selectedCpuNodeId.value).toBe('n2')
    expect(store.selectedGpuUuids.value.size).toBe(0)
  })

  it('llama.cpp 不自动预选推荐卡(CPU / GPU 由用户显式选)', () => {
    setupCpuLane()
    store.inspect.value = makeInspect({
      recommended_placement: { gpu_uuids: ['b1'], node_ids: ['n2'], arch: 'nvidia' },
    })
    store.applyRecommendedSelection()
    expect(store.selectedGpuUuids.value.size).toBe(0)
  })

  /** 带逐层表 + 各节点可用内存的预检(默认 CPU 落点挑选依据) */
  function withCpuMemory(availByNode: Record<string, number>) {
    store.inspect.value = makeInspect({
      memory: {
        weight_gb: 10,
        kv_gb: 2,
        total_layers: 8,
        tiers: [{ offload_layers: 0, ram_gb: 10, vram_gb: 0, feasible: true }],
        scenario: 'cpu_only',
        recommended_tier: 0,
        vram_avail_gb: 0,
        ram_avail_gb: 60,
        kv_source: 'formula',
      },
      node_support: [
        { node_id: 'n1', hostname: 'n1', selectable: false, reason: null, memory_gb: 64, available_memory_gb: availByNode.n1 },
        { node_id: 'n2', hostname: 'n2', selectable: true, reason: null, memory_gb: 64, available_memory_gb: availByNode.n2 },
      ],
    })
  }

  it('默认优先选中 CPU:挑「内存装得下」且余量最大的节点(逐份等价展开)', () => {
    setupCpuLane()
    withCpuMemory({ n1: 12, n2: 50 }) // 需求 = 0 档 ram_gb 10(cpu_only 口径已含 KV)
    store.applyRecommendedSelection()
    expect(store.selectedCpuNodeId.value).toBe('n2')
    expect(store.cpuMode.value).toBe(true)
  })

  it('没有节点装得下时不预选 CPU 落点', () => {
    setupCpuLane()
    withCpuMemory({ n1: 5, n2: 6 })
    store.applyRecommendedSelection()
    expect(store.selectedCpuNodeId.value).toBeNull()
  })

  it('没有逐层表(模型缺形状元数据)时不自动选 CPU 落点', () => {
    setupCpuLane()
    store.applyRecommendedSelection()
    expect(store.selectedCpuNodeId.value).toBeNull()
  })

  it('用户取消 CPU 落点后不再自动重选', () => {
    setupCpuLane()
    withCpuMemory({ n1: 12, n2: 50 })
    store.applyRecommendedSelection()
    expect(store.selectedCpuNodeId.value).toBe('n2')

    store.toggleCpuNode(nodeOf('n2'))
    expect(store.selectedCpuNodeId.value).toBeNull()
    store.applyRecommendedSelection()
    expect(store.selectedCpuNodeId.value).toBeNull()
  })

  it('选中 CPU 节点后点 GPU 卡即切到显存部署(自动取消 CPU 落点)', () => {
    setupCpuLane()
    store.toggleCpuNode(nodeOf('n1'))
    expect(store.selectedCpuNodeId.value).toBe('n1')
    expect(store.cpuMode.value).toBe(true)
    // CPU 部署下 GPU 卡仍可点:点了就切部署(不再要求先取消 CPU 节点)
    expect(store.isGpuSelectable(gpuOf('b1'))).toBe(true)
    store.toggleGpu(gpuOf('b1'))
    expect(store.selectedCpuNodeId.value).toBeNull()
    expect([...store.selectedGpuUuids.value]).toEqual(['b1'])

    // 再点节点卡片切回 CPU:清空已选卡
    store.toggleCpuNode(nodeOf('n1'))
    expect(store.selectedCpuNodeId.value).toBe('n1')
    expect(store.selectedGpuUuids.value.size).toBe(0)
    store.toggleCpuNode(nodeOf('n1'))
    expect(store.selectedCpuNodeId.value).toBeNull()
  })

  it('CPU 模式:拓扑为单机单实例,GPU 侧规则不报错(逐份钉齐后)', () => {
    setupCpuLane()
    store.form.replicas = 2
    // 逐份钉定:每份各钉一个节点(同节点可承载多份)
    store.toggleCpuNode(nodeOf('n1'))
    store.setActiveReplica(1)
    store.toggleCpuNode(nodeOf('n2'))
    expect(store.cpuNodeSelections.value).toEqual(['n1', 'n2'])
    expect(store.derivedTopology.value).toEqual({
      tp: 1,
      pp: 1,
      replicas: 2,
      nnodes: 1,
      max_model_len: 8 * 1024,
    })
    expect(store.topologyRuleError.value).toBeNull()
    expect(store.pairingPreview.value.pairs).toEqual([])
  })

  it('CPU 模式:有份未钉定时 topologyRuleError 报错并禁提交', () => {
    setupCpuLane()
    store.form.replicas = 2
    store.toggleCpuNode(nodeOf('n1')) // 只钉份 1,份 2 空
    expect(store.topologyRuleError.value).toBe('副本2 还未选择 CPU 落点节点')
    expect(store.cpuPinComplete.value).toBe(false)
  })

  it('逐份钉定互不覆盖:切换激活副本各自钉定/取消,同节点可承载多份', () => {
    setupCpuLane()
    store.form.replicas = 3
    store.toggleCpuNode(nodeOf('n1')) // 份 1 → n1
    store.setActiveReplica(1)
    store.toggleCpuNode(nodeOf('n2')) // 份 2 → n2
    store.setActiveReplica(2)
    store.toggleCpuNode(nodeOf('n1')) // 份 3 → n1(同节点多份)
    expect(store.cpuNodeSelections.value).toEqual(['n1', 'n2', 'n1'])
    expect(store.cpuPinComplete.value).toBe(true)

    // 取消份 2:仅本份受影响
    store.setActiveReplica(1)
    store.toggleCpuNode(nodeOf('n2'))
    expect(store.cpuNodeSelections.value).toEqual(['n1', null, 'n1'])
    expect(store.cpuMode.value).toBe(true)
    expect(store.cpuPinComplete.value).toBe(false)
  })

  it('removeReplica 按索引 splice 钉定不错位(删中间份)', () => {
    setupCpuLane()
    store.form.replicas = 3
    store.toggleCpuNode(nodeOf('n1')) // 份 1 → n1
    store.setActiveReplica(1)
    store.toggleCpuNode(nodeOf('n2')) // 份 2 → n2
    store.setActiveReplica(2)
    store.toggleCpuNode(nodeOf('n1')) // 份 3 → n1

    expect(store.removeReplica(1)).toBe(true) // 删钉 n2 的中间份
    expect(store.cpuNodeSelections.value).toEqual(['n1', 'n1']) // 原份 3 的 n1 钉定保持,不错位
    expect(store.form.replicas).toBe(2)
  })

  it('推荐回填:空份按 replica_plans 逐份预填,用户动过的份不顶回', () => {
    setupCpuLane()
    withCpuPlacement(
      {
        node_ids: ['n1'],
        replica_plans: [
          {
            seq: 1,
            node_ids: ['n1'],
            arch: 'cpu',
            units: [{ role: 'full', gpu_uuids: [], fit: true, headroom_gb: null }],
            fit: true,
            issues: [],
          },
          {
            seq: 2,
            node_ids: ['n2'],
            arch: 'cpu',
            units: [{ role: 'full', gpu_uuids: [], fit: true, headroom_gb: null }],
            fit: true,
            issues: [],
          },
        ],
      },
      {
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
      },
    )
    store.form.replicas = 2
    // 份 2 用户已动过(钉了 n1 又取消 = touched)
    store.setActiveReplica(1)
    store.toggleCpuNode(nodeOf('n1'))
    store.toggleCpuNode(nodeOf('n1'))
    expect(store.cpuNodeSelections.value).toEqual([null, null])

    store.applyRecommendedSelection()
    // 份 1 按 plan 回填 n1;份 2 用户动过,不顶回 plan 的 n2
    expect(store.cpuNodeSelections.value).toEqual(['n1', null])
  })

  it('新增空份在重检后自动回填推荐落点', () => {
    setupCpuLane()
    store.form.replicas = 2
    withCpuPlacement(
      {
        node_ids: ['n1'],
        replica_plans: [
          {
            seq: 1,
            node_ids: ['n1'],
            arch: 'cpu',
            units: [{ role: 'full', gpu_uuids: [], fit: true, headroom_gb: null }],
            fit: true,
            issues: [],
          },
          {
            seq: 2,
            node_ids: ['n2'],
            arch: 'cpu',
            units: [{ role: 'full', gpu_uuids: [], fit: true, headroom_gb: null }],
            fit: true,
            issues: [],
          },
        ],
      },
      {
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
      },
    )
    store.applyRecommendedSelection()
    expect(store.cpuNodeSelections.value).toEqual(['n1', 'n2'])
    expect(store.cpuPinComplete.value).toBe(true)
  })

  it('提交 payload:allow_cpu + cpu_only + 逐份钉定 node_ids,不传卡集', async () => {
    setupCpuLane()
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.name = 'cpu-dep'
    store.form.replicas = 2
    store.toggleCpuNode(nodeOf('n1'))
    store.setActiveReplica(1)
    store.toggleCpuNode(nodeOf('n2'))
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()

    const spec = vi.mocked(createDeploy).mock.calls[0][0].spec
    expect(spec?.allow_cpu).toBe(true)
    // 节点有可用卡时 auto 会解成显存档,必须显式钉 cpu_only
    expect(spec?.offload_preference).toBe('cpu_only')
    expect(spec?.kv_cache).toBeUndefined()  // KV 落位不可配,CPU 部署不再带该字段
    expect(spec?.gpu_selector).toEqual({ node_ids: ['n1', 'n2'] })
    expect(spec?.topology).toMatchObject({ tp: 1, nnodes: 1, replicas: 2 })
  })

  it('提交时剔除预检回显的系统参数行,只回传用户新增/改过的行', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    // 全量预检回显:含系统生成项(占位模型路径/端口)——这些不能当用户参数回传,否则覆盖真实值
    vi.mocked(inspectDeployment).mockResolvedValue(
      makeInspect({
        resolved: {
          image: 'reg/vllm:latest',
          args: ['--model', '<model-7>', '--port', '8000'],
          env: {},
          protected_overlap: [],
        },
      }),
    )
    await store.runFinalInspect()
    store.addArg()
    store.editArgs.value[store.editArgs.value.length - 1] = '--threads 8'

    createDeploy.mockResolvedValue({ deployment_id: 'd1' })
    await store.submitCreate()

    const spec = vi.mocked(createDeploy).mock.calls[0][0].spec
    expect(spec?.engine_params?.extra_args).toEqual(['--threads', '8'])
  })

  it('CPU 模式下预检请求带 allow_cpu/cpu_only/逐份钉定 node_ids', async () => {    setupCpuLane()
    store.form.modelId = 7
    store.form.clusterId = 3
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
    store.toggleCpuNode(nodeOf('n1'))

    await store.loadInspect(true)

    const payload = vi.mocked(inspectDeployment).mock.calls[0][0]
    expect(payload.allow_cpu).toBe(true)
    expect(payload.offload_preference).toBe('cpu_only')
    expect(payload.kv_cache).toBeUndefined()  // KV 落位不可配
    expect(payload.gpu_selector).toEqual({ node_ids: ['n1'] })
  })

  it('CPU 未配齐时基础预检不发 selector(自动 binpack 口径供回填)', async () => {
    setupCpuLane()
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.replicas = 2
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())
    store.toggleCpuNode(nodeOf('n1')) // 份 2 未钉

    await store.loadInspect(true)

    const payload = vi.mocked(inspectDeployment).mock.calls[0][0]
    expect(payload.allow_cpu).toBe(true)
    expect(payload.offload_preference).toBe('cpu_only')
    expect(payload.gpu_selector).toBeUndefined()
  })

  it('CPU 未配齐时 runFinalInspect/submitCreate 直接返回不请求', async () => {
    setupCpuLane()
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.replicas = 2
    store.toggleCpuNode(nodeOf('n1')) // 份 2 未钉

    expect(await store.runFinalInspect()).toBeNull()
    expect(await store.submitCreate()).toBeUndefined()
    expect(createDeploy).not.toHaveBeenCalled()
  })

  it('全量预检:该节点内存装不下纯 CPU 时阻断(后端会静默回退显存档)', async () => {
    setupCpuLane()
    store.form.modelId = 7
    store.form.clusterId = 3
    store.toggleCpuNode(nodeOf('n1'))
    vi.mocked(inspectDeployment).mockResolvedValue(
      makeInspect({
        memory: {
          weight_gb: 40,
          kv_gb: 8,
          total_layers: 32,
          tiers: [{ offload_layers: 32, ram_gb: 12, vram_gb: 40, feasible: true }],
          scenario: 'gpu_only',
          recommended_tier: 0,
          vram_avail_gb: 80,
          ram_avail_gb: 8,
          kv_source: 'formula',
        },
      }),
    )

    const result = await store.runFinalInspect()

    expect(result?.deployable).toBe(false)
    expect(result?.issues[0]).toContain('纯 CPU')
  })

  it('引擎服务不了该模型(格式不匹配)时不给 CPU 落点', () => {
    setupCpuLane()
    // llama.cpp 仅支持 gguf;safetensors 模型 → 无 CPU 部署(与后端 supported_formats 同源)
    store.modelOptions.value = [
      { label: 'st-model', value: 7, raw: { model_type: 'llm', format: 'safetensors' } },
    ] as unknown as typeof store.modelOptions.value
    store.form.modelId = 7
    expect(store.engineServesModel.value).toBe(false)
    expect(store.engineSupportsCpu.value).toBe(false)
    expect(store.isCpuNodeSelectable(nodeOf('n1'))).toBe(false)
  })

  it('格式匹配(gguf)时正常提供 CPU 落点', () => {
    setupCpuLane()
    store.modelOptions.value = [
      { label: 'gguf-model', value: 7, raw: { model_type: 'llm', format: 'gguf' } },
    ] as unknown as typeof store.modelOptions.value
    store.form.modelId = 7
    expect(store.engineServesModel.value).toBe(true)
    expect(store.isCpuNodeSelectable(nodeOf('n1'))).toBe(true)
  })

  it('非 cpu 引擎(如 vLLM)不产生 CPU 落点', () => {
    setupCpuLane()
    store.engines.value = [makeEngine({ engine: 'vllm', supported_gpu_archs: ['nvidia'] })]
    store.form.engine = 'vllm'
    expect(store.engineSupportsCpu.value).toBe(false)
    expect(store.isCpuNodeSelectable(nodeOf('n1'))).toBe(false)
  })

  // ---------- 纯 CPU 档落点对接(2026-09-28 inspect recommended_placement 节点级落点) ----------

  /** 带后端 binpack 落点的纯 CPU 档预检(node_ids 单节点 + 逐份 replica_plans,无卡) */
  function withCpuPlacement(
    placement: Partial<InspectRecommendedPlacement> | null,
    overrides: Partial<InspectDeploymentResult> = {},
  ) {
    store.inspect.value = makeInspect({
      recommended_placement:
        placement === null
          ? null
          : {
              gpu_uuids: [],
              node_ids: [],
              arch: 'cpu',
              ...placement,
            },
      ...overrides,
    })
  }

  it('cpu_only 档:默认预选回填后端 binpack 落点节点', () => {
    setupCpuLane()
    withCpuPlacement(
      { node_ids: ['n2'], replica_plans: [] },
      {
        memory: {
          weight_gb: 10,
          kv_gb: 2,
          total_layers: 8,
          tiers: [{ offload_layers: 0, ram_gb: 12, vram_gb: 0, feasible: true }],
          scenario: 'cpu_only',
          recommended_tier: 0,
          vram_avail_gb: 0,
          ram_avail_gb: 6,
          kv_source: 'formula',
        },
      },
    )
    // 落点权威源是后端 node_ids(即使前端启发式因内存算不出:n2 只剩 6GB 装不下 12GB 需求)
    store.applyRecommendedSelection()
    expect(store.selectedCpuNodeId.value).toBe('n2')
  })

  it('cpu_only 档空落点:不预选,noPlacementReason 透传后端容量阻断文案', () => {
    setupCpuLane()
    withCpuPlacement(
      { node_ids: [], replica_plans: [] },
      {
        deployable: false,
        issues: ['节点 n1 可用内存 6GB,不足纯 CPU 部署需求 12GB'],
        memory: {
          weight_gb: 10,
          kv_gb: 2,
          total_layers: 8,
          tiers: [{ offload_layers: 0, ram_gb: 12, vram_gb: 0, feasible: true }],
          scenario: 'cpu_only',
          recommended_tier: 0,
          vram_avail_gb: 0,
          ram_avail_gb: 6,
          kv_source: 'formula',
        },
      },
    )
    store.applyRecommendedSelection()
    expect(store.selectedCpuNodeId.value).toBeNull()
    expect(store.noPlacementReason.value).toContain('不足纯 CPU 部署需求')
  })

  it('cpu_only 档空落点且 issues 无内存文案:按 0 档需求兜底提示', () => {
    setupCpuLane()
    withCpuPlacement(
      { node_ids: [], replica_plans: [] },
      {
        memory: {
          weight_gb: 10,
          kv_gb: 2,
          total_layers: 8,
          tiers: [{ offload_layers: 0, ram_gb: 12, vram_gb: 0, feasible: true }],
          scenario: 'cpu_only',
          recommended_tier: 0,
          vram_avail_gb: 0,
          ram_avail_gb: 6,
          kv_source: 'formula',
        },
      },
    )
    expect(store.noPlacementReason.value).toContain('需约 12GB')
  })

  it('cpu_only 档有落点:noPlacementReason 为空(有逐份规划/节点集即有落点)', () => {
    setupCpuLane()
    withCpuPlacement(
      {
        node_ids: ['n2'],
        replica_plans: [
          {
            seq: 1,
            node_ids: ['n2'],
            arch: 'cpu',
            units: [{ role: 'full', gpu_uuids: [], fit: true, headroom_gb: null }],
            fit: true,
            issues: [],
          },
        ],
      },
      {
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
      },
    )
    expect(store.noPlacementReason.value).toBeNull()
  })

  it('cpuMode 下已选 CPU 节点仍参与空落点判定(钉定节点内存不足即时阻断)', () => {
    setupCpuLane()
    store.toggleCpuNode(nodeOf('n1'))
    withCpuPlacement(
      { node_ids: [], replica_plans: [] },
      {
        memory: {
          weight_gb: 10,
          kv_gb: 2,
          total_layers: 8,
          tiers: [{ offload_layers: 0, ram_gb: 12, vram_gb: 0, feasible: true }],
          scenario: 'cpu_only',
          recommended_tier: 0,
          vram_avail_gb: 0,
          ram_avail_gb: 6,
          kv_source: 'formula',
        },
      },
    )
    expect(store.cpuMode.value).toBe(true)
    expect(store.noPlacementReason.value).toBeTruthy()
  })
})

describe('applyRecommendedSelection 推荐选卡回填', () => {
  function setup(placementUuids: string[] | null) {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [
        makeGpu('gpu-1', 'n1'),
        makeGpu('gpu-2', 'n1'),
        makeGpu('gpu-3', 'n2'),
        makeGpu('gpu-off', 'n2', { status: 'offline' }),
      ],
    })
    store.inspect.value = makeInspect({
      recommended_placement: placementUuids
        ? { gpu_uuids: placementUuids, node_ids: ['n1'], arch: 'nvidia' }
        : null,
    })
  }

  it('无已选且推荐卡全部可选时按推荐预选', () => {
    setup(['gpu-1', 'gpu-2'])
    store.applyRecommendedSelection()
    expect([...store.selectedGpuUuids.value].sort()).toEqual(['gpu-1', 'gpu-2'])
  })

  it('已有选择时不覆盖', () => {
    setup(['gpu-1', 'gpu-2'])
    store.selectedGpuUuids.value = new Set(['gpu-3'])
    store.applyRecommendedSelection()
    expect([...store.selectedGpuUuids.value]).toEqual(['gpu-3'])
  })

  it('推荐集合含不可选卡(离线)时不预选,留给用户手动处理', () => {
    setup(['gpu-1', 'gpu-off'])
    store.applyRecommendedSelection()
    expect(store.selectedGpuUuids.value.size).toBe(0)
  })

  it('无 recommended_placement 时不预选', () => {
    setup(null)
    store.applyRecommendedSelection()
    expect(store.selectedGpuUuids.value.size).toBe(0)
  })

  /** 逐份规划 fixture:按份给卡(份内单节点) */
  function makePlans(lists: string[][]) {
    const nodeOf = (uuid: string) => (uuid.startsWith('gpu-1') || uuid.startsWith('gpu-2') ? 'n1' : 'n2')
    return lists.map((uuids, index) => ({
      seq: index + 1,
      node_ids: [...new Set(uuids.map(nodeOf))],
      arch: 'nvidia',
      units: [{ role: 'full', gpu_uuids: uuids, fit: true, headroom_gb: 1 }],
      fit: true,
      issues: [],
    }))
  }

  it('份 1 已手选、份 2 空:仅空份按对应 plan 精确回填,手选保持', () => {
    setup(null)
    store.form.replicas = 2
    store.replicaSelections.value = [['gpu-1'], []]
    store.inspect.value = makeInspect({
      recommended_placement: {
        gpu_uuids: ['gpu-1', 'gpu-2', 'gpu-3'],
        node_ids: ['n1', 'n2'],
        arch: 'nvidia',
        replica_plans: makePlans([['gpu-1', 'gpu-2'], ['gpu-3']]),
      },
    })
    store.applyRecommendedSelection()
    // 份 1 手选(1 张)不按 plan(2 张)覆盖;份 2 按 plan2 回填
    expect(store.replicaSelections.value).toEqual([['gpu-1'], ['gpu-3']])
  })

  it('plan 卡被其它份手选占用:该份跳过不预选,留给用户手动处理', () => {
    setup(null)
    store.form.replicas = 2
    store.replicaSelections.value = [['gpu-1'], []]
    // plan2 引用了已被份 1 手选的 gpu-1 → 份 2 无法完整回填,保持空
    store.inspect.value = makeInspect({
      recommended_placement: {
        gpu_uuids: ['gpu-1', 'gpu-3'],
        node_ids: ['n1', 'n2'],
        arch: 'nvidia',
        replica_plans: makePlans([['gpu-1'], ['gpu-1', 'gpu-3']]),
      },
    })
    store.applyRecommendedSelection()
    expect(store.replicaSelections.value).toEqual([['gpu-1'], []])
  })
})

// ---------- 按副本选卡(卡片式副本) ----------

describe('按副本选卡(卡片式副本)', () => {
  /** n1/n2 各 2 张 nvidia 卡 + n2 1 张 cann 卡(架构桶用) */
  function setupReplicas() {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [
        makeGpu('a1', 'n1'),
        makeGpu('a2', 'n1'),
        makeGpu('b1', 'n2'),
        makeGpu('b2', 'n2'),
        makeGpu('c1', 'n2', { arch: 'cann' }),
      ],
    })
    store.inspect.value = null
  }

  function gpuOf(uuid: string): TopologyGpu {
    const found = store.topology.value?.gpus.find((item) => item.uuid === uuid)
    if (!found) throw new Error(`missing fixture gpu: ${uuid}`)
    return found
  }

  it('addReplica/removeReplica/setActiveReplica:份结构增删切换并同步 form.replicas', () => {
    setupReplicas()
    expect(store.replicaSelections.value).toEqual([[]])
    expect(store.form.replicas).toBe(1)

    expect(store.addReplica()).toBe(true)
    expect(store.replicaSelections.value).toEqual([[], []])
    expect(store.form.replicas).toBe(2)
    // 新份自动激活
    expect(store.activeReplicaIndex.value).toBe(1)

    store.setActiveReplica(0)
    expect(store.activeReplicaIndex.value).toBe(0)

    // 移除激活份(末位):激活指针迁移到相邻份
    store.setActiveReplica(1)
    expect(store.removeReplica(1)).toBe(true)
    expect(store.replicaSelections.value).toEqual([[]])
    expect(store.activeReplicaIndex.value).toBe(0)
    expect(store.form.replicas).toBe(1)
    // 至少保留一份
    expect(store.removeReplica(0)).toBe(false)
    // 移除激活份之前的份:激活指针前移
    store.addReplica()
    store.addReplica()
    store.setActiveReplica(2)
    store.removeReplica(0)
    expect(store.activeReplicaIndex.value).toBe(1)
    expect(store.replicaSelections.value.length).toBe(2)
  })

  it('外部直接写 form.replicas:份结构按长度对齐(截断/补空份)', () => {
    setupReplicas()
    store.form.replicas = 3
    expect(store.replicaSelections.value).toEqual([[], [], []])
    store.toggleGpu(gpuOf('a1'))
    store.form.replicas = 2
    // 截断保留前两份,激活指针 clamp
    expect(store.replicaSelections.value).toEqual([['a1'], []])
    store.form.replicas = 0 // 非法值按 1 兜底
    expect(store.form.replicas).toBe(1)
  })

  it('toggleGpu 作用于激活份;切换份后选卡落新份,份间互斥', () => {
    setupReplicas()
    store.toggleGpu(gpuOf('a1'))
    expect(store.replicaSelections.value).toEqual([['a1']])
    expect([...store.selectedGpuUuids.value]).toEqual(['a1'])

    store.addReplica()
    // 份 2 视角:a1 已属份 1 → 禁选并给原因
    expect(store.isGpuSelectable(gpuOf('a1'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('a1'))).toBe('已被副本1选中')
    // 点已属其它份的卡:忽略(toggleGpu 走可选性门控)
    store.toggleGpu(gpuOf('a1'))
    expect(store.replicaSelections.value).toEqual([['a1'], []])
    // 选 n2 的卡入份 2
    store.toggleGpu(gpuOf('b1'))
    expect(store.replicaSelections.value).toEqual([['a1'], ['b1']])
    // 全量并集(提交口径)按份序拼接
    expect([...store.allSelectedUuids.value]).toEqual(['a1', 'b1'])
    // 切回份 1:a1 可再点(取消)
    store.setActiveReplica(0)
    store.toggleGpu(gpuOf('a1'))
    expect(store.replicaSelections.value).toEqual([[], ['b1']])
  })

  it('多副本份内桶约束:激活份确立桶后,其它节点/架构的卡禁选', () => {
    setupReplicas()
    store.addReplica()
    store.toggleGpu(gpuOf('b1'))
    expect(store.inspectDisabledReason(gpuOf('a1'))).toBe('须与本副本已选卡同节点同架构')
    expect(store.isGpuSelectable(gpuOf('a1'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('c1'))).toBe('须与本副本已选卡同节点同架构')
    // 同桶的 b2 可选
    expect(store.isGpuSelectable(gpuOf('b2'))).toBe(true)
    // 份 1(空)不受份 2 的桶约束
    store.setActiveReplica(0)
    expect(store.isGpuSelectable(gpuOf('a1'))).toBe(true)
  })

  it('topologyRuleError 多副本:空份/卡数不一致;等量且单桶通过', () => {
    setupReplicas()
    store.form.replicas = 2
    store.replicaSelections.value = [['a1'], []]
    expect(store.topologyRuleError.value).toContain('副本2 还未选卡')

    store.replicaSelections.value = [['a1'], ['b1', 'b2']]
    expect(store.topologyRuleError.value).toContain('卡数须一致')

    store.replicaSelections.value = [['a1'], ['b1']]
    expect(store.topologyRuleError.value).toBeNull()
    expect(store.derivedTopology.value).toMatchObject({ tp: 1, pp: 1, replicas: 2, nnodes: 1 })
  })

  it('多副本 submitCreate:gpu_uuids 按份序拼接,拓扑 tp=每份卡数', async () => {
    setupReplicas()
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.name = 'multi'
    store.form.replicas = 2
    store.replicaSelections.value = [
      ['a1', 'a2'],
      ['b1', 'b2'],
    ]
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()

    const spec = vi.mocked(createDeploy).mock.calls[0][0].spec
    expect(spec?.gpu_selector).toEqual({ gpu_uuids: ['a1', 'a2', 'b1', 'b2'] })
    expect(spec?.topology).toMatchObject({ tp: 2, pp: 1, replicas: 2, nnodes: 1 })
  })

  it('applyRecommendedSelection:replica_plans 份数对齐时逐份精确回填', () => {
    setupReplicas()
    store.form.replicas = 2
    store.inspect.value = makeInspect({
      recommended_placement: {
        gpu_uuids: ['a1', 'a2', 'b1'],
        node_ids: ['n1', 'n2'],
        arch: 'nvidia',
        replica_plans: [
          {
            seq: 1,
            node_ids: ['n1'],
            arch: 'nvidia',
            units: [{ role: 'full', gpu_uuids: ['a1', 'a2'], fit: true, headroom_gb: 1 }],
            fit: true,
            issues: [],
          },
          {
            seq: 2,
            node_ids: ['n2'],
            arch: 'nvidia',
            units: [{ role: 'full', gpu_uuids: ['b1', 'b2'], fit: true, headroom_gb: 1 }],
            fit: true,
            issues: [],
          },
        ],
      },
    })
    store.applyRecommendedSelection()
    expect(store.replicaSelections.value).toEqual([
      ['a1', 'a2'],
      ['b1', 'b2'],
    ])
  })

  it('applyRecommendedSelection:份数不对齐回退并集入首份,已有选择不覆盖', () => {
    setupReplicas()
    store.inspect.value = makeInspect({
      recommended_placement: { gpu_uuids: ['a1', 'b1'], node_ids: ['n1'], arch: 'nvidia' },
    })
    store.applyRecommendedSelection()
    expect(store.replicaSelections.value).toEqual([['a1', 'b1']])

    // 已有手选:不覆盖
    store.replicaSelections.value = [['a2'], []]
    store.applyRecommendedSelection()
    expect(store.replicaSelections.value).toEqual([['a2'], []])
  })

  it('llama.cpp 多副本:全局单节点锁定(任一份有卡即锁全部署节点)', () => {
    setupReplicas()
    store.form.engine = 'llamacpp'
    store.form.replicas = 2
    store.replicaSelections.value = [['a1'], []]
    // 份 2 虽空,但部署已锁 n1 → n2 的卡不可选
    expect(store.isGpuSelectable(gpuOf('b1'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('b1'))).toBe('llama.cpp 仅支持单节点部署(已选 n1)')
    // 份 2 继续选 n1 的卡
    expect(store.isGpuSelectable(gpuOf('a2'))).toBe(true)
    // 强行构造跨节点(直接写状态):topologyRuleError 报跨机部署
    store.replicaSelections.value = [['a1'], ['b1']]
    expect(store.topologyRuleError.value).toContain('不支持跨机部署')
  })

  it('selectAll 多副本:按桶贪心自动分配各份等量,跨桶余数留在未选中态(不制造违规份)', () => {
    setupReplicas()
    store.form.replicas = 2
    // 可选桶:n1×2(nvidia) / n2×2(nvidia) / n2×1(cann) → 每份 2 张:份1=n1,份2=n2(nvidia);
    // cann 桶单张配不出整份 → 余数不并入任何份(塞入会违反份内同桶约束且难以点掉),
    // 留在未选中态由用户手选;两份各自成桶,规则校验通过
    store.selectAll()
    expect(store.replicaSelections.value).toEqual([
      ['a1', 'a2'],
      ['b1', 'b2'],
    ])
    expect(store.topologyRuleError.value).toBeNull()
  })

  it('已属激活份的卡永远可点掉:自动分配塞入的异桶卡可移除,不卡死', () => {
    setupReplicas()
    store.form.replicas = 2
    // 直接构造违规状态(模拟自动分配误塞/防御态):份1 含跨桶卡 c1
    store.replicaSelections.value = [
      ['a1', 'c1'],
      ['b1'],
    ]
    // 桶违规先于卡数校验提示,指引移除外来卡
    expect(store.topologyRuleError.value).toContain('同一节点且同架构')
    // c1 虽与 a1 不同桶,但已属激活份 → 可点(isGpuSelectable 放行取消勾选)
    expect(store.isGpuSelectable(gpuOf('c1'))).toBe(true)
    store.toggleGpu(gpuOf('c1'))
    expect(store.replicaSelections.value[0]).toEqual(['a1'])
  })

  it('reset:份结构复位单份空集', () => {
    setupReplicas()
    store.form.replicas = 3
    store.toggleGpu(gpuOf('a1'))
    store.reset()
    expect(store.replicaSelections.value).toEqual([[]])
    expect(store.activeReplicaIndex.value).toBe(0)
    expect(store.form.replicas).toBe(1)
  })
})

// ---------- Step1 写回与提交 ----------

describe('applyForm 级联清理', () => {
  function seed() {
    store.topology.value = makeTopology()
    store.inspect.value = makeInspect()
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    store.editArgs.value = ['--x']
    store.form.name = 'd'
    store.form.modelId = 1
    store.form.clusterId = 1
    store.form.engine = 'vllm'
  }

  it('换集群:拓扑/预检/已选 GPU 全部清空', () => {
    seed()
    store.applyForm({ ...store.form, clusterId: 2 })
    expect(store.topology.value).toBeNull()
    expect(store.inspect.value).toBeNull()
    expect(store.selectedGpuUuids.value.size).toBe(0)
    expect(store.form.clusterId).toBe(2)
  })

  it('换模型或最大上下文:清预检与已选,拓扑缓存保留', () => {
    seed()
    store.applyForm({ ...store.form, modelId: 9 })
    expect(store.inspect.value).toBeNull()
    expect(store.selectedGpuUuids.value.size).toBe(0)
    expect(store.topology.value).not.toBeNull()
  })

  it('仅换引擎:只清预检,已选与拓扑保留', () => {
    seed()
    store.applyForm({ ...store.form, engine: 'sglang' })
    expect(store.inspect.value).toBeNull()
    expect(store.selectedGpuUuids.value.size).toBe(1)
    expect(store.topology.value).not.toBeNull()
  })

  it('表单值无变化:状态全保留', () => {
    seed()
    store.applyForm({ ...store.form })
    expect(store.inspect.value).not.toBeNull()
    expect(store.selectedGpuUuids.value.size).toBe(1)
    expect(store.topology.value).not.toBeNull()
  })
})

describe('submitCreate payload 组装', () => {
  function seedForm() {
    store.form.name = 'dep-1'
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.engine = 'auto'
    store.form.maxModelLen = null
    store.form.imageTag = null
    store.topology.value = makeTopology({
      gpus: [makeGpu('gpu-1', 'n1'), makeGpu('gpu-2', 'n1')],
    })
    store.selectedGpuUuids.value = new Set(['gpu-1', 'gpu-2'])
  }

  it('engine=auto 时取 inspect 推荐引擎,并带拓扑/选卡;成功后暂存创建记录', async () => {
    seedForm()
    store.inspect.value = makeInspect() // 推荐 vllm
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()
    expect(createDeploy).toHaveBeenCalledTimes(1)
    expect(vi.mocked(createDeploy).mock.calls[0][0]).toMatchObject({
      cluster_id: 3,
      name: 'dep-1',
      model_id: 7,
      engine: 'vllm',
      spec: {
        topology: { tp: 2, nnodes: 1, replicas: 1, pp: 1 },
        gpu_selector: { gpu_uuids: ['gpu-1', 'gpu-2'] },
      },
    })
    // 默认不带 force / image_tag / engine_params;publish 默认自动发布(无定制字段)
    const payload = vi.mocked(createDeploy).mock.calls[0][0]
    expect(payload.force).toBeUndefined()
    expect(payload.spec?.image_tag).toBeUndefined()
    expect(payload.spec?.engine_params).toBeUndefined()
    expect(payload.spec?.publish).toEqual({ auto_publish: true })
    // 创建记录暂存:退出向导时携带供列表层打开部署详情
    expect(store.lastCreated.value).toMatchObject({ deployment_id: 'd1' })
  })

  it('发布配置注入 spec.publish:定制字段全量带上,空值字段不传', async () => {
    seedForm()
    store.publish.name = '  qwen-pro  '
    store.publish.modelType = 'llm'
    store.publish.description = '  '
    store.publish.tags = ['思考', '工具调用']
    store.publish.inputPrice = 2
    store.publish.outputPrice = 8
    store.publish.cachedInputPrice = null
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()
    // name trim;空白描述不传;缓存定价 null 不传
    expect(vi.mocked(createDeploy).mock.calls[0][0].spec?.publish).toEqual({
      auto_publish: true,
      name: 'qwen-pro',
      model_type: 'llm',
      tags: ['思考', '工具调用'],
      input_price_per_mtok: 2,
      output_price_per_mtok: 8,
    })
  })

  it('关闭自动发布时 spec.publish 只带 auto_publish:false', async () => {
    seedForm()
    store.publish.autoPublish = false
    store.publish.name = 'ignored'
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()
    expect(vi.mocked(createDeploy).mock.calls[0][0].spec?.publish).toEqual({
      auto_publish: false,
    })
  })

  it('编辑的参数行(trim+剔空行)拆回扁平 argv 作为 engine_params.extra_args', async () => {
    seedForm()
    store.editArgs.value = [
      '  --dtype auto  ',
      '',
      '   ',
      '--trust-remote-code',
      '--max-model-len=4096',
    ]
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()
    expect(vi.mocked(createDeploy).mock.calls[0][0].spec?.engine_params).toEqual({
      extra_args: ['--dtype', 'auto', '--trust-remote-code', '--max-model-len=4096'],
    })
  })

  it('编辑的 env 剔除空 key 并 trim 后作为 engine_params.env', async () => {
    seedForm()
    store.editEnv.value = [
      { key: '  HF_ENDPOINT ', value: 'https://mirror' },
      { key: '', value: 'ignored' },
    ]
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()
    expect(vi.mocked(createDeploy).mock.calls[0][0].spec?.engine_params).toEqual({
      env: { HF_ENDPOINT: 'https://mirror' },
    })
  })

  it('强制部署时 payload 带 force:true,选了镜像版本时带 spec.image_tag', async () => {
    seedForm()
    store.form.imageTag = '0.9.5'
    store.markForced()
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })

    await store.submitCreate()
    const payload = vi.mocked(createDeploy).mock.calls[0][0]
    expect(payload.force).toBe(true)
    expect(payload.spec?.image_tag).toBe('0.9.5')
  })

  it('未选卡(拓扑不完整)时返回 undefined 且不提交', async () => {
    seedForm()
    store.clearSelection()
    const result = await store.submitCreate()
    expect(result).toBeUndefined()
    expect(createDeploy).not.toHaveBeenCalled()
  })
})

describe('runFinalInspect 全量预检', () => {
  it('带 engine+topology+gpu_selector+image_tag 请求,回写可编辑 args/env 并复位 forced', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.engine = 'auto'
    store.form.imageTag = '0.9.5'
    store.form.maxModelLen = 4096
    store.inspect.value = makeInspect() // 推荐引擎 vllm
    store.topology.value = makeTopology({ gpus: [makeGpu('gpu-1', 'n1')] })
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    store.markForced()
    vi.mocked(inspectDeployment).mockResolvedValue(
      makeInspect({
        resolved: {
          image: 'reg/vllm:0.9.5',
          args: ['--tp', '1'],
          env: { NCCL_IFNAME: 'eth0' },
          protected_overlap: [],
        },
      }),
    )

    const result = await store.runFinalInspect()
    expect(vi.mocked(inspectDeployment).mock.calls[0][0]).toMatchObject({
      model_id: 7,
      cluster_id: 3,
      engine: 'vllm',
      gpu_selector: { gpu_uuids: ['gpu-1'] },
      image_tag: '0.9.5',
    })
    expect(vi.mocked(inspectDeployment).mock.calls[0][0].topology).toMatchObject({
      tp: 1,
      nnodes: 1,
      max_model_len: 4096,
    })
    expect(result?.resolved?.args).toEqual(['--tp', '1'])
    // 扁平 argv 按配对口径成行回显:flag 与值合并一行
    expect(store.editArgs.value).toEqual(['--tp 1'])
    expect(store.editEnv.value).toEqual([{ key: 'NCCL_IFNAME', value: 'eth0' }])
    // 全量预检会复位强制标记:后续提交不带 force(forced 未导出,经 payload 间接验证)
    createDeploy.mockResolvedValue({ deployment_id: 'd1' })
    await store.submitCreate()
    expect(vi.mocked(createDeploy).mock.calls[0][0].force).toBeUndefined()
  })

  it('预检失败返回 null 不抛异常', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.topology.value = makeTopology({ gpus: [makeGpu('gpu-1', 'n1')] })
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    vi.mocked(inspectDeployment).mockRejectedValue(new Error('boom'))

    await expect(store.runFinalInspect()).resolves.toBeNull()
    expect(store.inspectLoading.value).toBe(false)
  })
})

describe('基础预检缓存 loadInspect', () => {
  it('同键(modelId:clusterId:engine:maxModelLen)命中缓存不重复请求,换引擎重拉', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.engine = 'auto'
    store.form.maxModelLen = null
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())

    await store.loadInspect()
    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(1)

    store.form.engine = 'vllm'
    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(2)
  })
})

describe('KV Cache 精度(选项由后端声明,选中档写 extra_args)', () => {
  const KV_OPTIONS = [
    { value: '', label: '默认(不量化)', dtype_bytes: 2, kv_ratio: 1, args: [], hint: 'KV 按模型原始精度存储' },
    {
      value: 'fp8_e4m3',
      label: 'FP8 E4M3',
      dtype_bytes: 1,
      kv_ratio: 0.5,
      args: ['--kv-cache-dtype', 'fp8_e4m3'],
      hint: 'KV 约减半',
    },
  ]

  function seedEngine() {
    store.engines.value = [makeEngine({ kv_cache_dtype_options: KV_OPTIONS })]
    store.form.engine = 'vllm'
  }

  it('档位进缓存键并随预检传 extra_args;同档命中缓存不重复请求', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    seedEngine()
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())

    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(1)

    // 换档必须重拉:KV 字节数参与显存估算与推荐落点
    store.form.kvCacheDtype = 'fp8_e4m3'
    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(2)
    expect(vi.mocked(inspectDeployment).mock.calls[1][0].engine_params).toEqual({
      extra_args: ['--kv-cache-dtype', 'fp8_e4m3'],
    })

    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(2)
  })

  it('全量预检:flag 进 engine_params,resolved.args 回显进 Step3 可编辑列表', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    seedEngine()
    store.form.kvCacheDtype = 'fp8_e4m3'
    store.topology.value = makeTopology({ gpus: [makeGpu('gpu-1', 'n1')] })
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    vi.mocked(inspectDeployment).mockResolvedValue(
      makeInspect({
        resolved: {
          image: 'reg/vllm:latest',
          args: ['--kv-cache-dtype', 'fp8_e4m3'],
          env: {},
          protected_overlap: [],
        },
      }),
    )

    await store.runFinalInspect()
    expect(vi.mocked(inspectDeployment).mock.calls[0][0].engine_params).toEqual({
      extra_args: ['--kv-cache-dtype', 'fp8_e4m3'],
    })
    // 回显成行(flag 与值合并),用户仍可在参数列表手改,提交时以列表为准
    expect(store.editArgs.value).toEqual(['--kv-cache-dtype fp8_e4m3'])
  })

  it('提交 payload 含 KV 精度 flag:前端注入的用户层选项不被基线差集剔除', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.name = 'kv-dep'
    seedEngine()
    store.form.kvCacheDtype = 'fp8_e4m3'
    store.topology.value = makeTopology({ gpus: [makeGpu('gpu-1', 'n1')] })
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    // 三层合并回显:系统生成项 + KV flag(前端注入的用户层)并存
    vi.mocked(inspectDeployment).mockResolvedValue(
      makeInspect({
        resolved: {
          image: 'reg/vllm:latest',
          args: ['--model', '<model-7>', '--port', '8000', '--kv-cache-dtype', 'fp8_e4m3'],
          env: {},
          protected_overlap: [],
        },
      }),
    )
    await store.runFinalInspect()
    expect(store.editArgs.value).toEqual([
      '--model <model-7>',
      '--port 8000',
      '--kv-cache-dtype fp8_e4m3',
    ])

    createDeploy.mockResolvedValue({ deployment_id: 'd1' })
    await store.submitCreate()

    // KV flag 属用户配置,必须随创建 payload 下发(否则精度档端到端失效,容量估算与实跑不符)
    const spec = vi.mocked(createDeploy).mock.calls[0][0].spec
    expect(spec?.engine_params?.extra_args).toContain('--kv-cache-dtype')
    // 系统生成项仍被剔除(--model 占位路径回传会覆盖真实值)
    expect(spec?.engine_params?.extra_args).not.toContain('<model-7>')
  })

  it('用户从参数列表删除 KV flag 行:提交不再下发该 flag(用户编辑优先)', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.name = 'kv-dep'
    seedEngine()
    store.form.kvCacheDtype = 'fp8_e4m3'
    store.topology.value = makeTopology({ gpus: [makeGpu('gpu-1', 'n1')] })
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    vi.mocked(inspectDeployment).mockResolvedValue(
      makeInspect({
        resolved: {
          image: 'reg/vllm:latest',
          args: ['--kv-cache-dtype', 'fp8_e4m3'],
          env: {},
          protected_overlap: [],
        },
      }),
    )
    await store.runFinalInspect()
    // 用户在 Step3 删除整行 KV flag
    store.editArgs.value = []

    createDeploy.mockResolvedValue({ deployment_id: 'd1' })
    await store.submitCreate()

    const spec = vi.mocked(createDeploy).mock.calls[0][0].spec
    expect(spec?.engine_params?.extra_args ?? []).not.toContain('kv-cache-dtype')
  })
})

describe('loadInspect 竞态守卫(Step2 快速改副本数/上下文)', () => {
  it('并发触发只认最后一次响应:慢的旧响应不覆盖新结果', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.engine = 'auto'
    let resolveStale!: (value: InspectDeploymentResult) => void
    let resolveFresh!: (value: InspectDeploymentResult) => void
    vi.mocked(inspectDeployment)
      .mockImplementationOnce(
        () => new Promise<InspectDeploymentResult>((resolve) => (resolveStale = resolve)),
      )
      .mockImplementationOnce(
        () => new Promise<InspectDeploymentResult>((resolve) => (resolveFresh = resolve)),
      )

    const stale = store.loadInspect(true)
    store.form.replicas = 2
    const fresh = store.loadInspect(true)

    // 第二次(新)先返回,再放行第一次(旧):旧响应须被序号守卫丢弃
    resolveFresh(makeInspect())
    await fresh
    resolveStale(makeInspect({ recommendation: {
      engine: 'vllm',
      image: 'reg/stale:latest',
      topology: { tp: 9, pp: 1, replicas: 1, nnodes: 1, max_model_len: null },
      vram_per_gpu_gb: 99,
    } }))
    await stale
    expect(store.inspect.value?.recommendation.topology.tp).toBe(1)
    expect(store.inspect.value?.recommendation.vram_per_gpu_gb).toBe(40)
  })

  it('全量预检使在途基础预检失效:迟到的基础响应不覆盖全量结果', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.engine = 'auto'
    store.topology.value = makeTopology({ gpus: [makeGpu('gpu-1', 'n1')] })
    store.selectedGpuUuids.value = new Set(['gpu-1'])

    let resolveBase!: (value: InspectDeploymentResult) => void
    vi.mocked(inspectDeployment)
      .mockImplementationOnce(
        () => new Promise<InspectDeploymentResult>((resolve) => (resolveBase = resolve)),
      )
      .mockResolvedValueOnce(
        makeInspect({
          resolved: { image: 'reg/vllm:0.9.5', args: ['--tp', '1'], env: {}, protected_overlap: [] },
        }),
      )

    // 基础预检在途(挂起)时用户点下一步触发全量预检
    const base = store.loadInspect(true)
    await store.runFinalInspect()
    expect(store.inspect.value?.resolved).toBeTruthy()

    // 基础预检迟到响应:不得覆盖全量结果
    resolveBase(makeInspect())
    await base
    expect(store.inspect.value?.resolved).toBeTruthy()
  })
})

describe('reset 复位', () => {
  it('脏状态后全部复位,且镜像目录缓存失效(同引擎重开会重拉)', async () => {
    store.currentStep.value = 3
    store.form.name = 'x'
    store.form.modelId = 1
    store.form.clusterId = 1
    store.form.imageTag = '0.9.5'
    store.topology.value = makeTopology()
    store.inspect.value = makeInspect()
    store.selectedGpuUuids.value = new Set(['gpu-1'])
    store.markForced()
    store.editArgs.value = ['--x']
    store.lastCreated.value = { deployment_id: 'd1' } as typeof store.lastCreated.value
    store.publish.autoPublish = false
    store.publish.name = 'qwen-pro'
    store.publish.modelType = 'llm'
    store.publish.tags = ['思考']
    store.publish.inputPrice = 2

    store.reset()

    expect(store.currentStep.value).toBe(1)
    expect(store.form).toMatchObject({
      name: '',
      modelId: null,
      engine: 'auto',
      imageTag: null,
      clusterId: null,
      maxModelLen: 8 * 1024,
    })
    expect(store.publish).toMatchObject({
      autoPublish: true,
      name: '',
      modelType: null,
      description: '',
      tags: [],
      inputPrice: null,
      outputPrice: null,
      cachedInputPrice: null,
    })
    expect(store.topology.value).toBeNull()
    expect(store.inspect.value).toBeNull()
    expect(store.selectedGpuUuids.value.size).toBe(0)
    expect(store.editArgs.value).toEqual([])
    expect(store.editEnv.value).toEqual([])
    expect(store.lastCreated.value).toBeNull()

    // 镜像缓存键已清空:同引擎再次加载会重新请求
    vi.mocked(listInferenceImages).mockResolvedValue([makeImageEntry()])
    await store.loadImageVersions('vllm')
    expect(listInferenceImages).toHaveBeenCalledTimes(1)
  })
})

// ---------- 发布配置表单 → spec.publish ----------

describe('buildPublishSpec 表单值转 spec.publish', () => {
  function makePublish(overrides: Partial<WizardPublishValues> = {}): WizardPublishValues {
    return {
      autoPublish: true,
      name: '',
      modelType: null,
      description: '',
      tags: [],
      inputPrice: null,
      outputPrice: null,
      cachedInputPrice: null,
      ...overrides,
    }
  }

  it('空表单(默认开启)只带 auto_publish:true,空值字段不传', () => {
    expect(buildPublishSpec(makePublish())).toEqual({ auto_publish: true })
  })

  it('全量定制字段带上;name/description trim', () => {
    expect(
      buildPublishSpec(
        makePublish({
          name: ' qwen-pro ',
          modelType: 'llm',
          description: ' 描述 ',
          tags: ['视觉'],
          inputPrice: 1.5,
          outputPrice: 6,
          cachedInputPrice: 0.5,
        }),
      ),
    ).toEqual({
      auto_publish: true,
      name: 'qwen-pro',
      model_type: 'llm',
      description: '描述',
      tags: ['视觉'],
      input_price_per_mtok: 1.5,
      output_price_per_mtok: 6,
      cached_input_price_per_mtok: 0.5,
    })
  })

  it('关闭自动发布只返回 { auto_publish: false },其余字段忽略', () => {
    expect(
      buildPublishSpec(makePublish({ autoPublish: false, name: 'x', tags: ['思考'] })),
    ).toEqual({ auto_publish: false })
  })
})

// ---------- argv ↔ 参数行 转换(与约定 §3 配对口径一致) ----------

describe('toArgLines 扁平 argv → 配对行', () => {
  it('flag 与紧随的非 `-` 开头值合并一行', () => {
    expect(toArgLines(['--model', '/data/qwen', '--tp', '2'])).toEqual([
      '--model /data/qwen',
      '--tp 2',
    ])
  })

  it('无值布尔 flag 与 --k=v 形式独占一行(下一 token 以 `-` 开头视为新 flag)', () => {
    expect(toArgLines(['--trust-remote-code', '--max-model-len=4096', '--tp', '2'])).toEqual([
      '--trust-remote-code',
      '--max-model-len=4096',
      '--tp 2',
    ])
  })

  it('负数值跟在 flag 后仍合并一行(值不以 `-` 开头才配对的例外:负数以 `-` 开头,独占成行)', () => {
    // 口径如此:以 `-` 开头的下一 token 一律视为新 flag,负数值独占成行
    expect(toArgLines(['--gpu-memory-utilization', '0.9', '--offset', '-1'])).toEqual([
      '--gpu-memory-utilization 0.9',
      '--offset',
      '-1',
    ])
  })

  it('末尾 flag 无下一 token 时独占一行;空数组返回空', () => {
    expect(toArgLines(['--model', '/data/qwen', '--enable-prefix-caching'])).toEqual([
      '--model /data/qwen',
      '--enable-prefix-caching',
    ])
    expect(toArgLines([])).toEqual([])
  })
})

describe('argLineToTokens 参数行 → 扁平 token', () => {
  it('按首个空白拆分 flag 与值,值内空格保留', () => {
    expect(argLineToTokens('--model /data/my model')).toEqual(['--model', '/data/my model'])
    expect(argLineToTokens('--dtype auto')).toEqual(['--dtype', 'auto'])
  })

  it('单 token 行(--flag / --k=v)原样返回;空白行剔除为空数组', () => {
    expect(argLineToTokens('--trust-remote-code')).toEqual(['--trust-remote-code'])
    expect(argLineToTokens('--max-model-len=4096')).toEqual(['--max-model-len=4096'])
    expect(argLineToTokens('   ')).toEqual([])
    expect(argLineToTokens('')).toEqual([])
  })

  it('行首尾空白规整;值全为空白时只留 flag', () => {
    expect(argLineToTokens('  --dtype   auto  ')).toEqual(['--dtype', 'auto'])
    expect(argLineToTokens('--flag   ')).toEqual(['--flag'])
  })

  it('与 toArgLines 互逆(不含负数值的常规参数)', () => {
    const argv = ['--model', '/data/qwen', '--tp', '2', '--trust-remote-code']
    expect(toArgLines(argv).flatMap(argLineToTokens)).toEqual(argv)
  })
})

// ---------- 参数行必控前缀与推荐选项(preview_topology.engines 驱动) ----------

describe('isProtectedArgLine 参数行必控前缀判断', () => {
  it('行首前缀命中任一 protected flag 即只读', () => {
    expect(isProtectedArgLine('--model /data/qwen', ['--model', '--tp'])).toBe(true)
    expect(isProtectedArgLine('--tp 2', ['--model', '--tp'])).toBe(true)
    expect(isProtectedArgLine('--max-model-len=4096', ['--max-model-len'])).toBe(true)
  })

  it('未命中的行与空 flags 可编辑', () => {
    expect(isProtectedArgLine('--dtype auto', ['--model', '--tp'])).toBe(false)
    expect(isProtectedArgLine('--model', [])).toBe(false)
  })

  it('空白行不视为受保护;行首空白先规整', () => {
    expect(isProtectedArgLine('   ', ['--model'])).toBe(false)
    expect(isProtectedArgLine('  --model /data', ['--model'])).toBe(true)
  })
})

describe('paramSuggestionOptions 推荐参数 → AutoComplete 选项', () => {
  it('default 拼为完整参数行;填充值(label/value)不含中文,hint 经 render 保留原 node 追加展示', () => {
    const [option] = paramSuggestionOptions([
      { name: '--max-model-len', hint: '最大上下文', default: 4096 },
    ])
    // naive-ui AutoComplete 选中后按 label 填充输入框,label 必须为纯参数行
    expect(option.label).toBe('--max-model-len 4096')
    expect(option.value).toBe('--max-model-len 4096')
    expect(option.render).toBeTypeOf('function')
    // render 必须基于 info.node 重建:naive-ui 的 option 样式与点击处理都挂在默认 node 上,
    // 丢失 node 会导致下拉项样式丢失、点击无效(回归用例)
    const node = h('div', { class: 'n-base-select-option' }, h('div', {}, ['--max-model-len 4096']))
    const rendered = option.render!({ node, selected: false })
    const children = rendered.children as unknown[]
    // 首个子项保留原 option 内容节点
    expect(children[0]).toBe(node.children)
    // hint 仅作为追加的 span 子项,不混入 label/value;且带 z-1(悬浮态 option::before 的
    // pending 背景层为绝对定位,会盖住无 z-index 的 flex 子项,须与内置 __content 同层)
    const hintVNode = children[1] as { children?: unknown; props?: { class?: string } }
    expect(hintVNode.children).toBe('最大上下文')
    expect(hintVNode.props?.class).toContain('z-1')
  })

  it('default 为 null 只填充 name 本身', () => {
    expect(paramSuggestionOptions([{ name: '--trust-remote-code', default: null }])).toEqual([
      { label: '--trust-remote-code', value: '--trust-remote-code' },
    ])
  })

  it('choices 逐值展开为多条选项(default 被忽略),填充值为 name+空格+choice', () => {
    expect(
      paramSuggestionOptions([{ name: '--dtype', default: 'auto', choices: ['auto', 'half'] }]),
    ).toEqual([
      { label: '--dtype auto', value: '--dtype auto' },
      { label: '--dtype half', value: '--dtype half' },
    ])
  })

  it('无 default/choices 仅出 flag 本身;空串与 null 值跳过', () => {
    expect(paramSuggestionOptions([{ name: '--trust-remote-code' }])).toEqual([
      { label: '--trust-remote-code', value: '--trust-remote-code' },
    ])
    expect(
      paramSuggestionOptions([{ name: '--x', default: '' }, { name: '--y', choices: [null, ''] }]),
    ).toEqual([
      { label: '--x', value: '--x' },
      { label: '--y', value: '--y' },
    ])
  })

  it('name 缺失的项跳过;空数组/undefined/null 返回空', () => {
    expect(paramSuggestionOptions([{ hint: 'no name' }, { name: '  ' }])).toEqual([])
    expect(paramSuggestionOptions([])).toEqual([])
    expect(paramSuggestionOptions(undefined)).toEqual([])
    expect(paramSuggestionOptions(null)).toEqual([])
  })
})

describe('preview_engine 派生(protected_flags/param_suggestions)', () => {
  it('优先匹配生效引擎取必控 flag 与推荐选项,isArgProtected 据此判定', () => {
    store.inspect.value = makeInspect({
      preview_topology: makeTopology({
        engines: [
          { engine: 'sglang', display_name: 'SGLang', image: 'reg/sglang:latest' },
          {
            engine: 'vllm',
            display_name: 'vLLM',
            image: 'reg/vllm:latest',
            protected_flags: ['--model'],
            param_suggestions: [{ name: '--dtype', default: 'auto' }],
          },
        ],
      }),
    })
    // form.engine 默认 auto → 生效引擎取 inspect 推荐(vllm)
    expect(store.effectiveEngine.value).toBe('vllm')
    expect(store.protectedFlags.value).toEqual(['--model'])
    expect(store.isArgProtected('--model /data/qwen')).toBe(true)
    expect(store.isArgProtected('--dtype auto')).toBe(false)
    expect(store.argSuggestionOptions.value).toEqual([
      { label: '--dtype auto', value: '--dtype auto' },
    ])
  })

  it('未匹配到生效引擎时回退首项;preview_topology 缺失时为空集合不误伤', () => {
    store.inspect.value = makeInspect({
      preview_topology: makeTopology({
        engines: [
          {
            engine: 'sglang',
            display_name: 'SGLang',
            image: 'reg/sglang:latest',
            protected_flags: ['--tp'],
          },
        ],
      }),
    })
    // 推荐引擎为 vllm,列表只有 sglang → 回退首项
    expect(store.effectiveEngine.value).toBe('vllm')
    expect(store.protectedFlags.value).toEqual(['--tp'])
    expect(store.isArgProtected('--tp 2')).toBe(true)

    store.inspect.value = makeInspect()
    expect(store.protectedFlags.value).toEqual([])
    expect(store.argSuggestionOptions.value).toEqual([])
    expect(store.isArgProtected('--model /x')).toBe(false)
  })
})

describe('多机引擎能力门控(三级回落)与跨机 PP 拓扑', () => {
  function gpuOf(uuid: string): TopologyGpu {
    const found = store.topology.value?.gpus.find((item) => item.uuid === uuid)
    if (!found) throw new Error(`missing fixture gpu: ${uuid}`)
    return found
  }

  function setupTwoNodes() {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [makeGpu('a1', 'n1'), makeGpu('a2', 'n1'), makeGpu('b1', 'n2'), makeGpu('b2', 'n2')],
    })
    store.inspect.value = null
    store.form.engine = 'vllm'
  }

  it('engineSupportsMultinode 三级回落:inspect 版本感知 > 引擎字典 > 宽松兜底 true;llama 恒 false', () => {
    setupTwoNodes()
    // 两级都缺(旧后端):宽松兜底放行,由后端预检 pp 门控把关
    expect(store.engineSupportsMultinode.value).toBe(true)
    // 二级:引擎字典
    store.engines.value = [makeEngine({ supports_multinode: false })]
    expect(store.engineSupportsMultinode.value).toBe(false)
    // 一级:inspect 推荐(按 image_tag 版本折算)优先于字典
    store.inspect.value = makeInspect({
      recommendation: {
        engine: 'vllm',
        image: 'reg/vllm:0.10.0',
        topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 },
        vram_per_gpu_gb: 40,
        supports_multinode: true,
      },
    })
    expect(store.engineSupportsMultinode.value).toBe(true)
    // llama 单机引擎恒 false
    store.form.engine = 'llamacpp'
    expect(store.engineSupportsMultinode.value).toBe(false)
  })

  it('effectiveParallel 三级回落:inspect 词表 > 引擎字典 > 宽松兜底全含', () => {
    setupTwoNodes()
    expect(store.effectiveParallel.value).toEqual(['tp', 'pp', 'ep'])
    store.engines.value = [makeEngine({ supported_parallel: ['tp', 'pp'] })]
    expect(store.effectiveParallel.value).toEqual(['tp', 'pp'])
    store.inspect.value = makeInspect({
      recommendation: {
        engine: 'vllm',
        image: 'reg/sglang:0.4.2',
        topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 },
        vram_per_gpu_gb: 40,
        supported_parallel: ['tp'],
      },
    })
    expect(store.effectiveParallel.value).toEqual(['tp'])
  })

  it('2 节点各选 2 卡 + PP 可用(宽松兜底):tp=2 · pp=2 · nnodes=2(节点内 TP + 跨机 PP)', () => {
    setupTwoNodes()
    store.selectedGpuUuids.value = new Set(['a1', 'a2', 'b1', 'b2'])
    expect(store.derivedTopology.value).toEqual({
      tp: 2,
      pp: 2,
      replicas: 1,
      nnodes: 2,
      max_model_len: 8 * 1024,
    })
  })

  it('词表无 pp(字典级):维持旧形态跨机 TP(tp=4 · pp=1 · nnodes=2)', () => {
    setupTwoNodes()
    store.engines.value = [makeEngine({ supported_parallel: ['tp'] })]
    store.selectedGpuUuids.value = new Set(['a1', 'a2', 'b1', 'b2'])
    expect(store.derivedTopology.value).toMatchObject({ tp: 4, pp: 1, nnodes: 2 })
  })

  it('inspect 版本感知覆盖字典:镜像版本无多机时 blocked,跨节点选集报收敛提示', () => {
    setupTwoNodes()
    store.engines.value = [makeEngine({ supports_multinode: true })]
    store.inspect.value = makeInspect({
      recommendation: {
        engine: 'vllm',
        image: 'reg/vllm:0.7.3',
        topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: 8192 },
        vram_per_gpu_gb: 40,
        supports_multinode: false,
      },
    })
    store.selectedGpuUuids.value = new Set(['a1', 'b1'])
    expect(store.crossNodePpBlocked.value).toBe(true)
    expect(store.crossNodePpAvailable.value).toBe(false)
    expect(store.topologyRuleError.value).toContain('不支持多机部署')
  })

  it('cann 选卡:即使引擎支持多机也 blocked,跨节点报昇腾文案', () => {
    setupTwoNodes()
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [
        makeGpu('a1', 'n1', { arch: 'cann' }),
        makeGpu('a2', 'n1', { arch: 'cann' }),
        makeGpu('b1', 'n2', { arch: 'cann' }),
      ],
    })
    store.selectedGpuUuids.value = new Set(['a1', 'b1'])
    expect(store.selectionIsCann.value).toBe(true)
    expect(store.crossNodePpBlocked.value).toBe(true)
    expect(store.topologyRuleError.value).toContain('昇腾(cann)卡不支持多机部署')
  })

  it('须单机语境(引擎无多机):跨节点选集收敛到首卡节点,其它节点卡禁选带镜像版本文案', () => {
    setupTwoNodes()
    store.engines.value = [makeEngine({ supports_multinode: false })]
    // 直接置跨节点选集(组件 watch 收敛前的瞬态),narrowSelectionToSingleNode 逐份收敛
    store.selectedGpuUuids.value = new Set(['a1', 'b1'])
    store.narrowSelectionToSingleNode()
    expect([...store.selectedGpuUuids.value]).toEqual(['a1'])
    // 已确立节点后,其它节点的卡禁选并给出语境文案
    expect(store.isGpuSelectable(gpuOf('b1'))).toBe(false)
    expect(store.inspectDisabledReason(gpuOf('b1'))).toBe(
      '当前引擎或镜像版本不支持多机部署(已选 n1)',
    )
    // 同节点仍可继续选
    expect(store.isGpuSelectable(gpuOf('a2'))).toBe(true)
  })

  it('cann 语境收敛与禁选文案(昇腾)', () => {
    store.topology.value = makeTopology({
      nodes: [makeNode('n1'), makeNode('n2')],
      gpus: [
        makeGpu('a1', 'n1', { arch: 'cann' }),
        makeGpu('a2', 'n1', { arch: 'cann' }),
        makeGpu('b1', 'n2', { arch: 'cann' }),
      ],
    })
    store.inspect.value = null
    store.form.engine = 'vllm'
    store.selectedGpuUuids.value = new Set(['a1', 'b1'])
    store.narrowSelectionToSingleNode()
    expect([...store.selectedGpuUuids.value]).toEqual(['a1'])
    expect(store.inspectDisabledReason(gpuOf('b1'))).toBe('昇腾(cann)卡不支持多机部署(已选 n1)')
  })

  it('crossNodeModeTip:跨节点均分亮出形态(PP 可用/不可用),单节点与 blocked 瞬态不亮', () => {
    setupTwoNodes()
    // 未跨节点:不亮
    store.selectedGpuUuids.value = new Set(['a1', 'a2'])
    expect(store.crossNodeModeTip.value).toBe('')
    // 跨节点均分 + PP 可用(宽松兜底)
    store.selectedGpuUuids.value = new Set(['a1', 'a2', 'b1', 'b2'])
    expect(store.crossNodeModeTip.value).toBe('跨 2 节点 · 节点内 TP + 跨机 PP(每节点 2 卡)')
    // 词表无 pp:跨机 TP 形态提示
    store.engines.value = [makeEngine({ supported_parallel: ['tp'] })]
    expect(store.crossNodeModeTip.value).toBe('跨 2 节点 · 跨机 TP')
    // 引擎无多机(blocked):收敛前瞬态不亮误导文案
    store.engines.value = [makeEngine({ supported_parallel: ['tp', 'pp'], supports_multinode: false })]
    expect(store.crossNodeModeTip.value).toBe('')
  })
})

describe('预检缓存键含镜像版本(imageTag)', () => {
  it('同配置仅换镜像版本必须重新预检(能力折算按 image_tag 版本感知)', async () => {
    store.form.modelId = 7
    store.form.clusterId = 3
    store.form.engine = 'vllm'
    vi.mocked(inspectDeployment).mockResolvedValue(makeInspect())

    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(1)
    // 同配置重复:命中缓存
    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(1)
    // 换镜像版本:能力折算(supported_parallel/supports_multinode)随之变化,必须重拉
    store.form.imageTag = '0.10.0'
    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(2)
    expect(vi.mocked(inspectDeployment).mock.calls[1][0].image_tag).toBe('0.10.0')
    // 新版本命中缓存
    await store.loadInspect()
    expect(inspectDeployment).toHaveBeenCalledTimes(2)
  })
})
