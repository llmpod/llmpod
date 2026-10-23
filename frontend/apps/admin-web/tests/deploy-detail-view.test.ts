// @vitest-environment happy-dom
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
// VueFlow 依赖画布测量/ResizeObserver,测试环境以插槽桩替代:按节点类型渲染对应卡片插槽
// useVueFlow 的事件订阅收集到共享 handlers,便于用例直接触发节点/画布点击
const flowHandlers = vi.hoisted(
  () =>
    ({}) as {
      nodeClick?: (event: { node: { id: string; type: string } }) => void
      paneClick?: () => void
    },
)
/** 组件实际使用的 fitView spy(useVueFlow 多次调用共享同一实例,断言必须指向它) */
const fitViewSpy = vi.hoisted(() => vi.fn())
/** navigator.clipboard.writeText 桩(happy-dom 无实现;复制用例断言入参与失败分支) */
const clipboardWriteText = vi.hoisted(() => vi.fn())
vi.mock('@vue-flow/core', async () => {
  const { ref } = await import('vue')
  /** 模块级单例:测试可经 vi.mocked(useNodesInitialized)() 控制节点测量就绪状态 */
  const nodesInitializedRef = ref(true)
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
    }),
    useNodesInitialized: () => nodesInitializedRef,
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
  getInstanceCommand: vi.fn(),
  listDeploymentLogs: vi.fn(),
  listDeployments: vi.fn(),
  startDeployment: vi.fn(),
  stopDeployment: vi.fn(),
}))
vi.mock('@/api/topology', () => ({ getClusterTopology: vi.fn() }))
// 日志区「容器 stdout」Tab 懒渲染,首屏不会调;mock 以防后续用例切 Tab
vi.mock('@/api/tail-log', () => ({ tailContainerLog: vi.fn() }))
vi.mock('@/api/model', () => ({ listModels: vi.fn() }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useConfirm: vi.fn(),
    useDialog: vi.fn(),
    message: { success: vi.fn(), error: vi.fn() },
  }
})

import {
  deleteDeployment,
  getDeployment,
  getDeploymentNodeSummary,
  getInstanceCommand,
  listDeploymentLogs,
  startDeployment,
  stopDeployment,
} from '@/api/deployment'
import { HttpError } from '@repo/http-client'
import { getClusterTopology } from '@/api/topology'
import type { ClusterTopology } from '@/api/topology'
import { useConfirm, useDialog, message } from '@repo/hooks'
import { useNodesInitialized } from '@vue-flow/core'
import { NIcon } from 'naive-ui'
import { CheckmarkOutline, CopyOutline } from '@vicons/ionicons5'
import type { DeploymentDetailOut, DeploymentInstance, ModelDeploymentOut } from '@/api/deployment'
import DeployDetailView from '@/views/deploy/components/deploy-list/deploy-detail/index.vue'
import { buildDeployGraph, buildReplicaRows } from '@/views/deploy/components/deploy-list/deploy-detail/graph'
import type { DeploymentReplica } from '@/api/deployment'
import {
  buildNodeBadge,
  groupReplicas,
  pollIntervalMs,
  tagTypeDot,
} from '@/views/deploy/components/deploy-list/deploy-detail/useStore'

function makeRecord(overrides: Partial<ModelDeploymentOut> = {}): ModelDeploymentOut {
  return {
    deployment_id: 'd1',
    cluster_id: 1,
    name: 'prod-deploy',
    model_id: 7,
    engine: 'vllm',
    status: 'running',
    api_endpoint: null,
    last_error: null,
    phase: 'RUNNING',
    version: 1,
    spec: { topology: { tp: 2, pp: 1, replicas: 1, nnodes: 2, max_model_len: 8192 } },
    created_at: '2025-06-01T08:30:00Z',
    updated_at: '2025-06-01T09:00:00Z',
    ...overrides,
  } as ModelDeploymentOut
}

function makeInstance(overrides: Partial<DeploymentInstance> = {}): DeploymentInstance {
  return {
    instance_id: 'i0',
    deployment_id: 'd1',
    role: 'full',
    rank: 0,
    assigned_node: 'node-01',
    status: 'READY',
    container_id: 'c1',
    endpoint: 'http://10.0.0.1:8000',
    last_error: null,
    started_at: '2025-06-01T08:31:00Z',
    ready_at: '2025-06-01T08:32:00Z',
    stopped_at: null,
    ...overrides,
  } as DeploymentInstance
}

function makeDetail(overrides: Partial<DeploymentDetailOut> = {}): DeploymentDetailOut {
  return {
    deployment: makeRecord(),
    instances: [
      makeInstance(),
      makeInstance({
        instance_id: 'i1',
        rank: 1,
        assigned_node: 'node-02',
        status: 'RUNNING',
        endpoint: null,
        ready_at: null,
      }),
    ],
    endpoints: [
      {
        engine_type: 'vllm',
        protocol: 'openai',
        api_base: 'http://10.0.0.1:8000',
        path: '/v1/chat/completions',
        served_model: 'Qwen2.5-7B',
        context_window: 8192,
        operations: [],
        health_status: 'healthy',
      },
    ],
    ...overrides,
  } as DeploymentDetailOut
}

function mountView(record: ModelDeploymentOut) {
  return mount(DeployDetailView, {
    props: { record },
    attachTo: document.body,
  })
}

function makeTopology(): ClusterTopology {
  return {
    cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
    nodes: [
      { id: 'node-01', cluster_id: 1, hostname: 'host-01', ip: '10.0.1.1', status: 'online' },
      { id: 'node-02', cluster_id: 1, hostname: 'host-02', ip: '10.0.1.2', status: 'offline' },
    ],
    gpus: [
      {
        id: 1,
        uuid: 'GPU-aaa',
        node_id: 'node-01',
        index: 0,
        name: 'RTX 4090',
        arch: 'ada',
        total_vram_gb: 24,
        available_vram_gb: 12,
        util: 45,
        status: 'online',
      },
    ],
    deployments: [],
    engines: [],
    models: [],
  } as ClusterTopology
}

/** 按运行环境本地时区计算 formatDateTime 期望值(ISO → YYYY-MM-DD HH:mm) */
function localDateTime(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(useNodesInitialized)().value = true
  clipboardWriteText.mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: clipboardWriteText },
    configurable: true,
  })
  vi.mocked(getDeployment).mockResolvedValue(makeDetail())
  vi.mocked(getClusterTopology).mockResolvedValue(makeTopology())
  // 日志区时间线:默认空页(日志区用例在 deploy-detail-logs.test.ts 覆盖)
  vi.mocked(listDeploymentLogs).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 500,
    total_pages: 1,
  })
  vi.mocked(getDeploymentNodeSummary).mockResolvedValue([
    {
      node_id: 'node-01',
      node_hostname: 'host-01',
      node_ip: '10.0.1.1',
      step: 'pull_image',
      status: 'running',
      level: 'info',
      message: '镜像拉取中',
      updated_at: '2025-06-01T08:33:00Z',
    },
  ])
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('deploy-detail 纯函数', () => {
  it('groupReplicas:空实例 → 空组;单实例 → 仅 head', () => {
    expect(groupReplicas([], 1)).toEqual([])
    const groups = groupReplicas([makeInstance()], 1)
    expect(groups).toHaveLength(1)
    expect(groups[0].head?.rank).toBe(0)
    expect(groups[0].workers).toHaveLength(0)
  })

  it('groupReplicas:多副本 → 按 ceil(总数/副本数) 逐副本展开', () => {
    const instances = [0, 1, 2, 3].map((rank) => makeInstance({ instance_id: `i${rank}`, rank }))
    const groups = groupReplicas(instances, 2)
    expect(groups).toHaveLength(2)
    expect(groups[0].head?.rank).toBe(0)
    expect(groups[0].workers.map((item) => item.rank)).toEqual([1])
    expect(groups[1].head?.rank).toBe(2)
    expect(groups[1].workers.map((item) => item.rank)).toEqual([3])
  })

  it('pollIntervalMs:进行中 3s,终态 10s,扩缩容子态加密到 3s', () => {
    expect(pollIntervalMs('creating')).toBe(3000)
    expect(pollIntervalMs('deleting')).toBe(3000)
    expect(pollIntervalMs('running')).toBe(10000)
    expect(pollIntervalMs('failed')).toBe(10000)
    expect(pollIntervalMs('stopped')).toBe(10000)
    // SCALING 是 phase 子态(status 仍 running),轮询需加密跟进
    expect(pollIntervalMs('running', 'SCALING')).toBe(3000)
    expect(pollIntervalMs('running', 'RUNNING')).toBe(10000)
  })

  it('buildNodeBadge:步骤文案 + 步骤态配色 + 结构化字段;空 step 回退「未知」', () => {
    const badge = buildNodeBadge({
      node_id: 'node-01',
      node_hostname: 'host-01',
      node_ip: '10.0.1.1',
      step: 'pull_image',
      status: 'running',
      level: 'info',
      message: '镜像拉取中',
      updated_at: '2025-06-01T08:33:00Z',
    })
    expect(badge.label).toBe('拉取镜像')
    expect(badge.type).toBe('info')
    expect(badge.hostname).toBe('host-01')
    expect(badge.tip).toContain('running')
    expect(badge.tip).toContain('镜像拉取中')
    expect(badge.statusLabel).toBe('进行中')
    expect(badge.message).toBe('镜像拉取中')
    expect(badge.updatedAt).toBe(localDateTime('2025-06-01T08:33:00Z'))

    const empty = buildNodeBadge({
      node_id: null,
      node_hostname: null,
      node_ip: null,
      step: null,
      status: null,
      level: null,
      message: null,
      updated_at: null,
    })
    expect(empty.label).toBe('未知')
    expect(empty.type).toBe('default')
    expect(empty.hostname).toBeNull()
    expect(empty.statusLabel).toBe('等待中')
    expect(empty.updatedAt).toBe('—')
  })

  it('tagTypeDot:tag 配色 → 状态点语义色类', () => {
    expect(tagTypeDot('success')).toBe('bg-success')
    expect(tagTypeDot('info')).toBe('bg-info')
    expect(tagTypeDot('warning')).toBe('bg-warning')
    expect(tagTypeDot('error')).toBe('bg-error')
    expect(tagTypeDot('default')).toBe('bg-secondary')
  })
})

describe('buildDeployGraph 轮询稳定性', () => {
  /** 相同实体集合、不同输入顺序 → 每个节点落位一致(轮询刷新不打断浏览) */
  it('实例/集群拓扑返回顺序抖动:物理节点列与 GPU 落位不跳', () => {
    const instances = [
      makeInstance({ instance_id: 'i0', rank: 0, assigned_node: 'node-02' }),
      makeInstance({ instance_id: 'i1', rank: 1, assigned_node: 'node-01' }),
      makeInstance({ instance_id: 'i2', rank: 2, assigned_node: 'node-02' }),
      makeInstance({ instance_id: 'i3', rank: 3, assigned_node: 'node-01' }),
    ]
    const makeTopology = (gpuOrder: 'indexAsc' | 'indexDesc') =>
      ({
        cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
        nodes: [],
        // 同节点两张卡:数组顺序与物理 index 顺序刻意不一致
        gpus:
          gpuOrder === 'indexAsc'
            ? [
                { uuid: 'GPU-aaa', node_id: 'node-01', index: 0 },
                { uuid: 'GPU-bbb', node_id: 'node-01', index: 1 },
              ]
            : [
                { uuid: 'GPU-bbb', node_id: 'node-01', index: 1 },
                { uuid: 'GPU-aaa', node_id: 'node-01', index: 0 },
              ],
        deployments: [
          { deployment_id: 'd1', cluster_id: 1, name: 'prod', status: 'running', gpu_uuids: ['GPU-aaa', 'GPU-bbb'] },
        ],
        engines: [],
        models: [],
      }) as unknown as ClusterTopology

    const forward = buildDeployGraph({
      deployment: makeRecord(),
      instances,
      nodeSummary: [],
      modelName: 'Qwen2.5-7B',
      topology: makeTopology('indexAsc'),
    })
    const shuffled = buildDeployGraph({
      deployment: makeRecord(),
      instances: [...instances].reverse(),
      nodeSummary: [],
      modelName: 'Qwen2.5-7B',
      topology: makeTopology('indexDesc'),
    })

    const posOf = (graph: ReturnType<typeof buildDeployGraph>) =>
      new Map(graph.nodes.map((node) => [node.id, node.position]))
    const forwardPos = posOf(forward)
    expect(forwardPos.size).toBeGreaterThan(0)
    for (const node of shuffled.nodes) {
      expect(forwardPos.get(node.id)).toEqual(node.position)
    }

    // 物理节点列按 nodeId 稳定排序(不随实例首现顺序);同节点 GPU 按物理 index 稳定
    expect(forwardPos.get('phy-node-01')!.y).toBeLessThan(forwardPos.get('phy-node-02')!.y)
    expect(forwardPos.get('gpu-GPU-aaa')!.y).toBeLessThan(forwardPos.get('gpu-GPU-bbb')!.y)
  })
})

describe('buildReplicaRows 副本视图行', () => {
  /** replicas 层行(投影链权威;seq 为后端 0 基,展示层经 toReplicaSeq 转 1 基) */
  function makeReplica(overrides: Partial<DeploymentReplica> = {}): DeploymentReplica {
    return {
      replica_id: 'r-id-1',
      seq: 0,
      status: 'serving',
      engines: [
        { role: 'full', tp: 2, pp: 1, gpu_arch: 'ada', status: 'READY', shard_ids: ['i0', 'i1'] },
      ],
      endpoint_ids: [11],
      ...overrides,
    } as DeploymentReplica
  }

  it('有 replicas 层:shard_ids 反查分片归属,status/端点数取权威值', () => {
    const instances = [
      makeInstance({ instance_id: 'i0', rank: 0 }),
      makeInstance({ instance_id: 'i1', rank: 1 }),
      makeInstance({ instance_id: 'i2', rank: 2 }),
      makeInstance({ instance_id: 'i3', rank: 3 }),
    ]
    const rows = buildReplicaRows(instances, [makeReplica(), makeReplica({
      replica_id: 'r-id-2',
      seq: 1,
      status: 'provisioning',
      engines: [
        { role: 'full', tp: 2, pp: 1, gpu_arch: null, status: 'RUNNING', shard_ids: ['i2', 'i3'] },
      ],
      endpoint_ids: [],
    })], 2)
    expect(rows).toHaveLength(2)
    // seq 统一为 1 基展示序号(后端 0 基 + 1)
    expect(rows[0]).toMatchObject({
      replicaId: 'r-id-1',
      seq: 1,
      status: 'serving',
      endpointCount: 1,
      derived: false,
    })
    // 份内分片按 shard_ids 反查并以 rank 排序,最小 rank 为 head
    expect(rows[0].members.map((item) => item.instance_id)).toEqual(['i0', 'i1'])
    expect(rows[0].head?.instance_id).toBe('i0')
    expect(rows[1].status).toBe('provisioning')
    expect(rows[1].engines).toHaveLength(1)
  })

  it('replicas 层未覆盖的实例:合成 derived 兜底份(序号顺延),画布不丢分片', () => {
    const instances = [
      makeInstance({ instance_id: 'i0', rank: 0 }),
      makeInstance({ instance_id: 'i9', rank: 9 }),
    ]
    const rows = buildReplicaRows(instances, [makeReplica({ engines: [
      { role: 'full', tp: 1, pp: 1, gpu_arch: null, status: 'READY', shard_ids: ['i0'] },
    ] })], 1)
    // i0 归属权威份;i9 未被覆盖 → 附加 derived 份,seq 顺延为 2
    expect(rows).toHaveLength(2)
    expect(rows[0].members.map((item) => item.instance_id)).toEqual(['i0'])
    expect(rows[0].derived).toBe(false)
    expect(rows[1]).toMatchObject({ replicaId: null, seq: 2, derived: true })
    expect(rows[1].members.map((item) => item.instance_id)).toEqual(['i9'])
  })

  it('无 replicas 层:回退均分启发式,按实例态派生三态', () => {
    // 全就绪 → serving;含失败 → down;否则 provisioning
    const ready = [makeInstance({ instance_id: 'i0', rank: 0 }), makeInstance({ instance_id: 'i1', rank: 1 })]
    expect(buildReplicaRows(ready, null, 1)[0]).toMatchObject({
      replicaId: null,
      seq: 1,
      status: 'serving',
      derived: true,
      endpointCount: 0,
    })
    const failing = [makeInstance({ instance_id: 'i0', status: 'FAILED' })]
    expect(buildReplicaRows(failing, [], 1)[0].status).toBe('down')
    const mid = [makeInstance({ instance_id: 'i0', status: 'RUNNING' })]
    expect(buildReplicaRows(mid, undefined, 1)[0].status).toBe('provisioning')
  })
})

describe('buildDeployGraph 图装配', () => {
  it('单副本多机:部署 → 物理节点 → head → worker 四列,边按角色连接', () => {
    const { nodes, edges } = buildDeployGraph({
      deployment: makeRecord(),
      instances: makeDetail().instances,
      nodeSummary: [],
      modelName: 'Qwen2.5-7B',
    })

    // 节点:1 部署 + 2 实例 + 2 物理节点
    expect(nodes.filter((node) => node.type === 'deployment')).toHaveLength(1)
    expect(nodes.filter((node) => node.type === 'instance')).toHaveLength(2)
    expect(nodes.filter((node) => node.type === 'phy')).toHaveLength(2)

    // 物理节点列紧随部署列,head 次列、worker 末列
    const head = nodes.find((node) => node.id === 'inst-i0')!
    const worker = nodes.find((node) => node.id === 'inst-i1')!
    const phy = nodes.find((node) => node.id === 'phy-node-01')!
    expect(head.data.record).toMatchObject({ head: true, rank: 0, statusLabel: '就绪' })
    expect(worker.data.record).toMatchObject({ head: false, rank: 1, statusLabel: '运行中' })
    expect(phy.position.x).toBeLessThan(head.position.x)
    expect(head.position.x).toBeLessThan(worker.position.x)

    // 边:deploy→replica、replica→phy(静态)、phy→head/worker、head→worker;
    // 份级动画按份三态门控(fixture 含未就绪分片 → 份 provisioning,推理链路整体不流动);
    // 缺 replicas 层的推导份节点 id 为 replica-derived-{seq}
    const replicaEdge = edges.find((edge) => edge.target === 'replica-derived-1')!
    expect(replicaEdge.source).toBe('deployment-d1')
    expect(replicaEdge.animated).toBe(false)
    const phyEdge = edges.find((edge) => edge.target === 'phy-node-02')!
    expect(phyEdge.source).toBe('replica-derived-1')
    expect(phyEdge.animated).toBe(false)
    const headEdge = edges.find((edge) => edge.target === 'inst-i0')!
    expect(headEdge.source).toBe('phy-node-01')
    expect(headEdge.animated).toBe(false)
    const workerEdge = edges.find(
      (edge) => edge.source === 'phy-node-02' && edge.target === 'inst-i1',
    )!
    expect(workerEdge.animated).toBe(false)
    expect(
      edges.some((edge) => edge.source === 'inst-i0' && edge.target === 'inst-i1'),
    ).toBe(true)
  })

  it('份级动画:running 且份 serving 才流动;provisioning/down 份静态', () => {
    const serving = [
      makeInstance({ instance_id: 'i0', rank: 0 }),
      makeInstance({ instance_id: 'i1', rank: 1 }),
    ]
    const servingGraph = buildDeployGraph({
      deployment: makeRecord(),
      instances: serving,
      nodeSummary: [],
      modelName: '#7',
    })
    expect(servingGraph.edges.find((edge) => edge.target === 'replica-derived-1')!.animated).toBe(true)
    expect(servingGraph.edges.find((edge) => edge.target === 'inst-i0')!.animated).toBe(true)
    expect(
      servingGraph.edges.some((edge) => edge.source === 'inst-i0' && edge.target === 'inst-i1' && edge.animated),
    ).toBe(true)

    // 份内有分片失败 → down;启动中 → provisioning:两种态都不流动
    const down = buildDeployGraph({
      deployment: makeRecord(),
      instances: [makeInstance({ instance_id: 'i0', status: 'FAILED' })],
      nodeSummary: [],
      modelName: '#7',
    })
    expect(down.edges.find((edge) => edge.target === 'replica-derived-1')!.animated).toBe(false)
    const provisioning = buildDeployGraph({
      deployment: makeRecord(),
      instances: [makeInstance({ instance_id: 'i0', status: 'RUNNING' })],
      nodeSummary: [],
      modelName: '#7',
    })
    expect(provisioning.edges.find((edge) => edge.target === 'replica-derived-1')!.animated).toBe(false)
  })

  it('多副本:2 head 同列;物理节点列固定紧随部署列;非 running 部署边不动画', () => {
    const instances = [0, 1].map((rank) =>
      makeInstance({ instance_id: `i${rank}`, rank, assigned_node: `node-0${rank + 1}` }),
    )
    const { nodes, edges } = buildDeployGraph({
      deployment: makeRecord({
        status: 'stopped',
        spec: { topology: { tp: 1, pp: 1, replicas: 2, nnodes: 1, max_model_len: null } },
      }),
      instances,
      nodeSummary: [],
      modelName: '#7',
    })

    // 2 副本 → 2 个 head,无 worker;副本列节点带三态徽标与引擎摘要位
    const heads = nodes.filter((node) => node.type === 'instance')
    expect(heads).toHaveLength(2)
    expect(heads.every((node) => node.data.record.head)).toBe(true)
    expect(heads[0].data.record.replicaCount).toBe(2)
    expect(nodes.filter((node) => node.type === 'replica')).toHaveLength(2)
    // 实例卡片所属份序号从 1 起,多副本副标题展示 r{seq}
    expect(heads[0].data.record.replicaSeq).toBe(1)
    expect(heads[1].data.record.replicaSeq).toBe(2)

    // 物理节点列固定在副本列之后(x = 620),不随 worker 缺省移动;heads 同列
    const phy = nodes.find((node) => node.type === 'phy')!
    expect(phy.position.x).toBe(620)
    expect(new Set(heads.map((node) => node.position.x)).size).toBe(1)

    // stopped:所有边静态
    expect(edges.every((edge) => !edge.animated)).toBe(true)
  })

  it('部署节点负载:状态/子态/拓扑结构化字段/模型名;launching 按 creating 渲染', () => {
    const { nodes } = buildDeployGraph({
      deployment: makeRecord({ status: 'creating', phase: 'PULL_IMAGE' }),
      instances: [],
      nodeSummary: [],
      modelName: 'Qwen2.5-7B',
    })
    expect(nodes).toHaveLength(1)
    expect(nodes[0].data.record).toMatchObject({
      name: 'prod-deploy',
      modelName: 'Qwen2.5-7B',
      engine: 'vllm',
      statusLabel: '部署中',
      phaseLabel: '拉取镜像',
      // 拓扑属性结构化(藏于详情面板分行展示)
      topology: { tp: 2, pp: 1, replicas: 1, nnodes: 2, maxModelLen: 8192 },
      createdAt: localDateTime('2025-06-01T08:30:00Z'),
      updatedAt: localDateTime('2025-06-01T09:00:00Z'),
    })

    const launching = buildDeployGraph({
      deployment: makeRecord({ status: 'launching' as never, phase: 'PENDING' }),
      instances: [],
      nodeSummary: [],
      modelName: '#7',
    })
    expect(launching.nodes[0].data.record.statusLabel).toBe('部署中')
    expect(launching.nodes[0].data.record.phaseLabel).toBe('排队中')
  })

  it('phaseLabel 边界:非 creating 为 null;未知子态(不在流水线内)回退原始值', () => {
    const running = buildDeployGraph({
      deployment: makeRecord({ status: 'running', phase: 'RUNNING' }),
      instances: [],
      nodeSummary: [],
      modelName: 'm',
    })
    expect(running.nodes[0].data.record.phaseLabel).toBeNull()

    const unknown = buildDeployGraph({
      deployment: makeRecord({ status: 'creating', phase: 'SOMETHING_NEW' }),
      instances: [],
      nodeSummary: [],
      modelName: 'm',
    })
    // 未知子态:文案回退原始值(顶部进度条不高亮任何步骤,current 逻辑在控制器侧)
    expect(unknown.nodes[0].data.record.phaseLabel).toBe('SOMETHING_NEW')
  })

  it('无拓扑信息的部署:topology 为 null(面板整块显示 --)', () => {
    const { nodes } = buildDeployGraph({
      deployment: makeRecord({ spec: {} }),
      instances: [],
      nodeSummary: [],
      modelName: '#7',
    })
    expect(nodes[0].data.record.topology).toBeNull()
  })

  it('实例负载:含容器/起停时间等详情面板字段', () => {
    const { nodes } = buildDeployGraph({
      deployment: makeRecord(),
      instances: makeDetail().instances,
      nodeSummary: [],
      modelName: '#7',
    })
    const head = nodes.find((node) => node.id === 'inst-i0')!
    expect(head.data.record).toMatchObject({
      containerId: 'c1',
      startedAt: localDateTime('2025-06-01T08:31:00Z'),
      readyAt: localDateTime('2025-06-01T08:32:00Z'),
      stoppedAt: '—',
    })
    const worker = nodes.find((node) => node.id === 'inst-i1')!
    expect(worker.data.record).toMatchObject({ containerId: 'c1', stoppedAt: '—' })
  })

  it('物理节点徽标:按 node_id 对齐 logs/nodes 摘要;无摘要为 null', () => {
    const { nodes } = buildDeployGraph({
      deployment: makeRecord(),
      instances: makeDetail().instances,
      nodeSummary: [
        {
          node_id: 'node-01',
          node_hostname: 'host-01',
          node_ip: '10.0.1.1',
          step: 'pull_image',
          status: 'failed',
          level: 'error',
          message: '拉取失败',
          updated_at: '2025-06-01T08:33:00Z',
        },
      ],
      modelName: '#7',
    })
    const withBadge = nodes.find((node) => node.id === 'phy-node-01')!
    expect(withBadge.data.record.badge).toMatchObject({
      label: '拉取镜像',
      type: 'error',
      hostname: 'host-01',
    })
    expect(withBadge.data.record.statusDot).toBe('bg-error')

    const noBadge = nodes.find((node) => node.id === 'phy-node-02')!
    expect(noBadge.data.record.badge).toBeNull()
    expect(noBadge.data.record.statusDot).toBe('bg-secondary')

    // 承载实例引用:node-01 上是 head(rank 0),node-02 上是 worker(rank 1);单副本标 r1
    expect(withBadge.data.record.instances).toEqual([
      { label: '#0 full · 副本1 · Head', statusLabel: '就绪' },
    ])
    expect(noBadge.data.record.instances).toEqual([
      { label: '#1 full · 副本1 · Worker', statusLabel: '运行中' },
    ])
  })

  it('GPU 列纵向不重叠:多节点多卡任意相邻卡间距 ≥ 140(大于卡高约 110);列 x 错开 worker 列', () => {
    const instances = [
      makeInstance({ instance_id: 'i0', rank: 0, assigned_node: 'node-01' }),
      makeInstance({ instance_id: 'i1', rank: 1, assigned_node: 'node-02' }),
    ]
    const topology = {
      cluster: { id: 1, name: 'c1', type: 'docker', status: 'active' },
      nodes: [],
      gpus: [
        { uuid: 'GPU-a1', node_id: 'node-01', index: 0 },
        { uuid: 'GPU-a2', node_id: 'node-01', index: 1 },
        { uuid: 'GPU-b1', node_id: 'node-02', index: 0 },
        { uuid: 'GPU-b2', node_id: 'node-02', index: 1 },
      ],
      deployments: [
        {
          deployment_id: 'd1',
          cluster_id: 1,
          name: 'prod',
          status: 'running',
          gpu_uuids: ['GPU-a1', 'GPU-a2', 'GPU-b1', 'GPU-b2'],
        },
      ],
      engines: [],
      models: [],
    } as unknown as ClusterTopology

    const { nodes } = buildDeployGraph({
      deployment: makeRecord(),
      instances,
      nodeSummary: [],
      modelName: 'Qwen2.5-7B',
      topology,
    })
    const gpuNodes = nodes
      .filter((node) => node.type === 'gpu')
      .sort((a, b) => a.position.y - b.position.y)
    expect(gpuNodes).toHaveLength(4)
    // GPU 卡(NodeCard 显存/利用率两块)渲染高约 110px,相邻卡间距必须大于卡高才不上下重叠
    for (let i = 1; i < gpuNodes.length; i++) {
      expect(gpuNodes[i].position.y - gpuNodes[i - 1].position.y).toBeGreaterThanOrEqual(140)
    }
    // 分组顺序:node-01 组在 node-02 组之上,组内按物理 index
    expect(gpuNodes.map((node) => node.id)).toEqual([
      'gpu-GPU-a1',
      'gpu-GPU-a2',
      'gpu-GPU-b1',
      'gpu-GPU-b2',
    ])
    // GPU 列 x 不与 worker 列(x=1220 + 卡宽 180 = 右缘 1400)水平重叠
    expect(gpuNodes[0].position.x).toBeGreaterThanOrEqual(1400)
  })
})

describe('DeployDetailView 渲染', () => {
  it('挂载后拉详情 + 节点摘要,画布渲染部署/实例/物理节点卡片;低频数据不占卡片', async () => {
    const wrapper = mountView(makeRecord())
    // 等待详情数据真正渲染(接口 resolve 后再断言)
    await vi.waitFor(() => expect(document.body.textContent).toContain('Qwen2.5-7B'))
    expect(getDeployment).toHaveBeenCalledWith('d1')
    expect(getDeploymentNodeSummary).toHaveBeenCalledWith('d1')
    await nextTick()

    const text = document.body.textContent ?? ''
    expect(text).toContain('部署详情-prod-deploy')
    expect(text).toContain('http://10.0.0.1:8000/v1/chat/completions')
    // 实例卡片:head/worker
    expect(text).toContain('#0 full')
    expect(text).toContain('#1 full')
    expect(text).toContain('Head')
    expect(text).toContain('Worker')
    // 物理节点卡片 + 步骤徽标(subtitle)
    expect(text).toContain('node-01')
    expect(text).toContain('node-02')
    expect(text).toContain('拉取镜像')
    // 卡片瘦身:拓扑属性/实例端点与就绪时间/徽标 tip 已藏入详情面板,画布不再展示
    expect(text).not.toContain('tp=2 · pp=1')
    expect(text).not.toContain('TP 并行')
    expect(text).not.toContain('容器 ID')
    expect(text).not.toContain(localDateTime('2025-06-01T08:32:00Z'))
    expect(text).not.toContain('镜像拉取中')
    wrapper.unmount()
  })

  it('详情拉取失败:回退列表行快照渲染部署卡片', async () => {
    vi.mocked(getDeployment).mockRejectedValue(new Error('boom'))
    const wrapper = mountView(makeRecord({ name: 'fallback-deploy' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()
    await nextTick()

    const text = document.body.textContent ?? ''
    expect(text).toContain('部署详情-fallback-deploy')
    // 部署卡片以行快照渲染(模型名未加载回退 #id)
    expect(text).toContain('#7')
    wrapper.unmount()
  })

  it('首次视图适配等详情返回:节点测量先于详情返回时不在残缺单节点图上 fit', async () => {
    // 真实时序:列表快照构建的单节点图先完成测量(早于详情接口返回),修复前此时已被 fitView 消耗
    let resolveDetail!: (value: DeploymentDetailOut) => void
    vi.mocked(getDeployment).mockReturnValue(
      new Promise((resolve) => {
        resolveDetail = resolve
      }),
    )
    vi.mocked(useNodesInitialized)().value = false
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    // 单节点残缺图测量完成(详情尚未返回):不得 fit
    vi.mocked(useNodesInitialized)().value = true
    await nextTick()
    expect(fitViewSpy).not.toHaveBeenCalled()

    // 详情返回 → 完整图渲染并测量就绪 → 首次 fitView 一次
    resolveDetail(makeDetail())
    await vi.waitFor(() => expect(document.body.textContent).toContain('#0 full'))
    await nextTick()
    expect(fitViewSpy).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('返回按钮触发 exit', async () => {
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const backBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('返回'),
    )!
    backBtn.click()
    await nextTick()
    expect(wrapper.emitted('exit')).toHaveLength(1)
    wrapper.unmount()
  })

  it('推理端点与 served 模型支持复制:入参正确且成功后图标短暂切换为对勾', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountView(makeRecord())
      await vi.waitFor(() => expect(document.body.textContent).toContain('Qwen2.5-7B'))

      const buttons = [...document.body.querySelectorAll('button')]
      const endpointBtn = buttons.find((btn) => btn.getAttribute('title') === '复制端点')!
      const modelBtn = buttons.find((btn) => btn.getAttribute('title') === '复制 served 模型')!
      expect(endpointBtn).toBeTruthy()
      expect(modelBtn).toBeTruthy()

      /** 端点区块两个复制按钮对应的 NIcon:初始均为 CopyOutline */
      const copyIcons = () =>
        wrapper
          .findAllComponents(NIcon)
          .filter(
            (icon) =>
              icon.props('component') === CopyOutline ||
              icon.props('component') === CheckmarkOutline,
          )
      const checkCount = () =>
        copyIcons().filter((icon) => icon.props('component') === CheckmarkOutline).length
      /** 当前显示对勾的按钮 title(单值 copiedKey,同一时刻仅一个对勾) */
      const checkedTitles = () =>
        copyIcons()
          .filter((icon) => icon.props('component') === CheckmarkOutline)
          .map((icon) => icon.element.closest('button')?.getAttribute('title'))
      expect(copyIcons()).toHaveLength(2)
      expect(checkCount()).toBe(0)

      // 复制端点:完整 URL(base + path);对勾出现在端点按钮
      // (copyToClipboard 经封装多一拍微任务,对勾断言用条件等待而非固定一拍 nextTick)
      endpointBtn.click()
      await vi.waitFor(() =>
        expect(clipboardWriteText).toHaveBeenCalledWith('http://10.0.0.1:8000/v1/chat/completions'),
      )
      await vi.waitFor(() => expect(checkedTitles()).toEqual(['复制端点']))

      // 复制 served 模型:模型名;对勾转移到 served 模型按钮
      modelBtn.click()
      await vi.waitFor(() => expect(clipboardWriteText).toHaveBeenCalledWith('Qwen2.5-7B'))
      await vi.waitFor(() => expect(checkedTitles()).toEqual(['复制 served 模型']))

      // 1.5s 后对勾恢复为复制图标
      await vi.advanceTimersByTimeAsync(1500)
      await nextTick()
      expect(checkCount()).toBe(0)
      expect(copyIcons()).toHaveLength(2)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('复制失败:提示手动复制', async () => {
    clipboardWriteText.mockRejectedValueOnce(new Error('denied'))
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('Qwen2.5-7B'))

    const endpointBtn = [...document.body.querySelectorAll('button')].find(
      (btn) => btn.getAttribute('title') === '复制端点',
    )!
    endpointBtn.click()
    await vi.waitFor(() => expect(message.error).toHaveBeenCalledWith('复制失败,请手动复制'))
    wrapper.unmount()
  })
})

describe('DeployDetailView 节点详情面板', () => {
  it('点击部署节点:右上角展示拓扑属性等详情', async () => {
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('Qwen2.5-7B'))
    flowHandlers.nodeClick?.({ node: { id: 'deployment-d1', type: 'deployment' } })
    await nextTick()

    const text = document.body.textContent ?? ''
    expect(text).toContain('TP 并行')
    expect(text).toContain('上下文长度')
    expect(text).toContain('8192')
    expect(text).toContain(localDateTime('2025-06-01T08:30:00Z'))
    // 状态行按语义色上色(running → bg-success → text-success)
    expect(document.body.innerHTML).toContain('text-success')
    wrapper.unmount()
  })

  it('点击实例节点:右上角展示端点/容器/起停时间详情', async () => {
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('#0 full'))
    flowHandlers.nodeClick?.({ node: { id: 'inst-i0', type: 'instance' } })
    await nextTick()

    const text = document.body.textContent ?? ''
    expect(text).toContain('实例详情')
    expect(text).toContain('容器 ID')
    expect(text).toContain('c1')
    expect(text).toContain(localDateTime('2025-06-01T08:32:00Z'))
    wrapper.unmount()
  })

  it('点击物理节点:按需拉取集群拓扑资产,面板展示主机名/IP/GPU/承载实例/徽标消息', async () => {
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('node-01'))
    flowHandlers.nodeClick?.({ node: { id: 'phy-node-01', type: 'phy' } })
    // 物理节点卡片:按需拉取的资产(主机名/IP/GPU)已回填,卡片标题展示节点 hostname
    await vi.waitFor(() => expect(getClusterTopology).toHaveBeenCalledWith(1))
    await vi.waitFor(() => expect(document.body.textContent).toContain('10.0.1.1'))

    const text = document.body.textContent ?? ''
    expect(text).toContain('节点详情')
    expect(text).toContain('10.0.1.1')
    expect(text).toContain('RTX 4090')
    expect(text).toContain('#0 full · 副本1 · Head(就绪)')
    // 徽标消息从卡片 tip 移入面板分行展示
    expect(text).toContain('镜像拉取中')
    wrapper.unmount()
  })

  it('实例有最近错误:面板红字完整换行展示(不省略),卡片省略悬浮', async () => {
    vi.mocked(getDeployment).mockResolvedValue(
      makeDetail({
        instances: [makeInstance({ last_error: 'CUDA error: out of memory at kernel launch' })],
      }),
    )
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('#0 full'))
    flowHandlers.nodeClick?.({ node: { id: 'inst-i0', type: 'instance' } })
    await nextTick()

    const labelSpan = [...document.body.querySelectorAll('span')].find(
      (el) => el.textContent === '最近错误',
    )!
    const valueSpan = labelSpan.nextElementSibling as HTMLElement
    expect(valueSpan.textContent).toContain('CUDA error: out of memory at kernel launch')
    expect(valueSpan.className).toContain('text-error')
    expect(valueSpan.className).not.toContain('truncate')
    wrapper.unmount()
  })

  it('各层节点卡片「日志」入口:分别打开编排/容器/节点过滤弹窗', async () => {
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('#0 full'))
    const findBtn = (text: string) =>
      [...document.body.querySelectorAll('button')].find((btn) =>
        (btn.textContent ?? '').trim().includes(text),
      )!
    const click = (text: string) => {
      findBtn(text).click()
    }

    // 部署卡片:编排日志(时间线,不带节点过滤)
    click('编排日志')
    await nextTick()
    expect(vi.mocked(useDialog)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(useDialog).mock.calls[0]![0].props).toMatchObject({
      deploymentId: 'd1',
      nodeId: null,
    })

    // 实例卡片:容器日志(定位该实例)
    click('容器日志')
    await nextTick()
    expect(vi.mocked(useDialog)).toHaveBeenCalledTimes(2)
    expect(vi.mocked(useDialog).mock.calls[1]![0].props).toMatchObject({
      deploymentId: 'd1',
      initialInstanceId: 'i0',
    })

    // 物理节点卡片:节点日志(按 node_id 过滤)
    click('节点日志')
    await nextTick()
    expect(vi.mocked(useDialog)).toHaveBeenCalledTimes(3)
    expect(vi.mocked(useDialog).mock.calls[2]![0].props).toMatchObject({
      deploymentId: 'd1',
      nodeId: 'node-01',
    })
    wrapper.unmount()
  })

  it('实例详情面板「启动命令」入口:面板底部按钮打开命令弹窗(props 定位该实例);卡片不外露入口', async () => {
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('#0 full'))
    await nextTick()

    // 卡片不外露启动命令入口(低频诊断动作藏于详情面板)
    expect(
      [...document.body.querySelectorAll('button')].find((btn) =>
        (btn.textContent ?? '').trim().includes('启动命令'),
      ),
    ).toBeUndefined()

    // 点击实例节点 → 详情面板底部出现「启动命令」
    flowHandlers.nodeClick?.({ node: { id: 'inst-i0', type: 'instance' } })
    await nextTick()
    const panelBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      (btn.textContent ?? '').trim().includes('启动命令'),
    )!
    expect(panelBtn).toBeTruthy()
    panelBtn.click()
    await nextTick()

    expect(vi.mocked(useDialog)).toHaveBeenCalledTimes(1)
    const call = vi.mocked(useDialog).mock.calls[0]![0]
    expect(call.props).toMatchObject({ deploymentId: 'd1', instanceId: 'i0' })
    expect(call.title).toContain('启动命令')
    wrapper.unmount()

    // 非实例面板(部署节点)无该入口
    const wrapper2 = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('Qwen2.5-7B'))
    flowHandlers.nodeClick?.({ node: { id: 'deployment-d1', type: 'deployment' } })
    await nextTick()
    expect(
      [...document.body.querySelectorAll('button')].find((btn) =>
        (btn.textContent ?? '').trim().includes('启动命令'),
      ),
    ).toBeUndefined()
    wrapper2.unmount()
  })

  it('creating 期间:画布顶部固定渲染编排进度条;部署卡片不展示子态 tag', async () => {
    vi.mocked(getDeployment).mockResolvedValue(
      makeDetail({ deployment: makeRecord({ status: 'creating', phase: 'PULL_IMAGE' }) }),
    )
    const wrapper = mountView(makeRecord({ status: 'creating', phase: 'PULL_IMAGE' }))
    await vi.waitFor(() => expect(document.body.textContent).toContain('prod-deploy'))
    await nextTick()
    // 固定进度条:卡片标题 + 完整创建流水线步骤条;绝对定位于画布容器内(不随画布平移/缩放)
    expect(document.body.textContent).toContain('编排进度')
    const steps = document.body.querySelector('.n-steps')
    expect(steps?.textContent).toContain('拉取镜像')
    expect(steps?.querySelectorAll('.n-step')).toHaveLength(9)
    const bar = steps?.closest('div.absolute')
    expect(bar).toBeTruthy()
    // 默认水平居中(inset-x-0 + mx-auto);宽度不足时步骤条横向滚动兜底(min-w 保底不挤压)
    expect(bar?.className).toContain('inset-x-0')
    expect(bar?.className).toContain('mx-auto')
    expect(bar?.querySelector('.overflow-x-auto')).toBeTruthy()
    expect(steps?.className).toContain('min-w-[100rem]')
    // 部署卡片:子态 tag 已移除(子态由顶部进度条/详情面板/头部承载)
    const phaseTag = [...document.body.querySelectorAll('.n-tag')].find((el) =>
      el.textContent?.includes('拉取镜像'),
    )
    expect(phaseTag).toBeUndefined()
    wrapper.unmount()
  })

  it('非 creating:不渲染顶部编排进度条', async () => {
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('Qwen2.5-7B'))
    await nextTick()
    expect(document.body.querySelector('.n-steps')).toBeNull()
    wrapper.unmount()
  })

  it('点击画布空白/ESC 关闭详情面板', async () => {
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(document.body.textContent).toContain('Qwen2.5-7B'))
    flowHandlers.nodeClick?.({ node: { id: 'deployment-d1', type: 'deployment' } })
    await nextTick()
    expect(document.body.textContent).toContain('TP 并行')

    flowHandlers.paneClick?.()
    await nextTick()
    expect(document.body.textContent).not.toContain('TP 并行')

    flowHandlers.nodeClick?.({ node: { id: 'deployment-d1', type: 'deployment' } })
    await nextTick()
    expect(document.body.textContent).toContain('TP 并行')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(document.body.textContent).not.toContain('TP 并行')
    wrapper.unmount()
  })

  it('面板数据随轮询刷新:选中态存节点 id,下一轮数据回来面板同步更新', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountView(makeRecord())
      await vi.advanceTimersByTimeAsync(0)
      flowHandlers.nodeClick?.({ node: { id: 'inst-i0', type: 'instance' } })
      await nextTick()
      expect(document.body.textContent).toContain('服务端点')

      // 下一轮轮询返回新端点 → 面板同步(record 实时取,非选中时快照)
      vi.mocked(getDeployment).mockResolvedValue(
        makeDetail({ instances: [makeInstance({ endpoint: 'http://10.9.9.9:9000' })] }),
      )
      await vi.advanceTimersByTimeAsync(10000)
      await vi.waitFor(() => expect(document.body.textContent).toContain('http://10.9.9.9:9000'))
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('DeployDetailView 停止/启动/删除', () => {
  it('running 状态显示停止按钮、不显示启动按钮;确认后调接口并立即重刷一轮', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(stopDeployment).mockResolvedValue({ deployment_id: 'd1', status: 'stopped' })
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const buttons = [...document.body.querySelectorAll('button')]
    expect(buttons.find((btn) => btn.textContent?.includes('启动'))).toBeUndefined()
    const stopBtn = buttons.find((btn) => btn.textContent?.includes('停止'))!
    stopBtn.click()
    await vi.waitFor(() => expect(stopDeployment).toHaveBeenCalledWith('d1'))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已停止:prod-deploy'))
    // 停止成功立即手动刷一轮(不等轮询)
    await vi.waitFor(() =>
      expect(vi.mocked(getDeployment).mock.calls.length).toBeGreaterThanOrEqual(2),
    )
    wrapper.unmount()
  })

  it('failed 状态不显示停止按钮(失败已是停止终态),显示启动按钮;确认后调 start 并立即重刷一轮', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(startDeployment).mockResolvedValue({})
    vi.mocked(getDeployment).mockResolvedValue(
      makeDetail({ deployment: makeRecord({ status: 'failed' }) }),
    )
    const wrapper = mountView(makeRecord({ status: 'failed' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const buttons = [...document.body.querySelectorAll('button')]
    expect(buttons.find((btn) => btn.textContent?.includes('停止'))).toBeUndefined()
    const startBtn = buttons.find((btn) => btn.textContent?.includes('启动'))!
    expect(startBtn).toBeTruthy()

    startBtn.click()
    await vi.waitFor(() => expect(startDeployment).toHaveBeenCalledWith('d1', false))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已启动:prod-deploy'))
    // 启动成功立即手动刷一轮(不等轮询)
    await vi.waitFor(() =>
      expect(vi.mocked(getDeployment).mock.calls.length).toBeGreaterThanOrEqual(2),
    )
    wrapper.unmount()
  })

  it('启动遇 409 原卡被占:二次确认后带 force 强制启动;取消强制则不重试', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(startDeployment)
      .mockRejectedValueOnce(
        new HttpError({ type: 'http', status: 409, message: '原卡被部署 dep-x 占用' }),
      )
      .mockResolvedValue({})
    const wrapper = mountView(makeRecord({ status: 'stopped' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const startBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('启动'),
    )!
    startBtn.click()
    await vi.waitFor(() => expect(startDeployment).toHaveBeenCalledWith('d1', false))
    // 二次确认(强制启动)后带 force=true 重试
    await vi.waitFor(() => expect(startDeployment).toHaveBeenCalledWith('d1', true))
    expect(vi.mocked(useConfirm)).toHaveBeenCalledTimes(2)
    expect(vi.mocked(useConfirm).mock.calls[1]![0]).toMatchObject({ title: '强制启动' })
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已启动:prod-deploy'))
    wrapper.unmount()

    // 二次确认取消:不带 force 重试
    vi.mocked(useConfirm).mockReset()
    vi.mocked(startDeployment).mockReset()
    vi.mocked(useConfirm)
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('cancel'))
    vi.mocked(startDeployment).mockRejectedValue(
      new HttpError({ type: 'http', status: 409, message: '原卡被部署 dep-x 占用' }),
    )
    const wrapper2 = mountView(makeRecord({ status: 'stopped' }))
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()
    const startBtn2 = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('启动'),
    )!
    startBtn2.click()
    await vi.waitFor(() => expect(startDeployment).toHaveBeenCalledWith('d1', false))
    await nextTick()
    await nextTick()
    expect(startDeployment).toHaveBeenCalledTimes(1)
    wrapper2.unmount()
  })

  it('删除:确认后调接口并退出视图;取消不调用', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(deleteDeployment).mockResolvedValue({ deployment_id: 'd1', deleted: true })
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    const removeBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('删除'),
    )!
    removeBtn.click()
    await vi.waitFor(() => expect(deleteDeployment).toHaveBeenCalledWith('d1'))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已删除:prod-deploy'))
    expect(wrapper.emitted('exit')).toHaveLength(1)
    wrapper.unmount()

    // 取消:不调用删除接口
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))
    vi.mocked(deleteDeployment).mockClear()
    const wrapper2 = mountView(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()
    const removeBtn2 = [...document.body.querySelectorAll('button')].find((btn) =>
      btn.textContent?.includes('删除'),
    )!
    removeBtn2.click()
    await nextTick()
    await nextTick()
    expect(deleteDeployment).not.toHaveBeenCalled()
    wrapper2.unmount()
  })

  it('详情抽屉内删除:成功后退出详情视图(removed → exit 返回列表)', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(deleteDeployment).mockResolvedValue({ deployment_id: 'd1', deleted: true })
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    // 头部「详情」按钮展开抽屉(抽屉打开后按 deployment_id 重拉详情)
    const detailBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      (btn.textContent ?? '').trim().includes('详情'),
    )!
    detailBtn.click()
    await vi.waitFor(() =>
      expect(vi.mocked(getDeployment).mock.calls.length).toBeGreaterThanOrEqual(2),
    )
    await nextTick()

    // 抽屉内删除按钮(与头部删除同名,取渲染顺序最后一个:抽屉 teleport 挂 body 末尾)
    const removeBtn = [...document.body.querySelectorAll('button')]
      .filter((btn) => btn.textContent?.includes('删除'))
      .pop()!
    removeBtn.click()
    await vi.waitFor(() => expect(deleteDeployment).toHaveBeenCalledWith('d1'))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已删除:prod-deploy'))
    // 视图退出:emit exit(DeployList 据此 closeDetailView 回列表)
    expect(wrapper.emitted('exit')).toHaveLength(1)
    wrapper.unmount()
  })

  it('详情抽屉内停止:外部拓扑视图经 notify 联动立即刷新(不等轮询)', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(stopDeployment).mockResolvedValue({ deployment_id: 'd1', status: 'stopped' })
    const wrapper = mountView(makeRecord())
    await vi.waitFor(() => expect(getDeployment).toHaveBeenCalled())
    await nextTick()

    // 头部「详情」按钮展开抽屉(抽屉打开后自己重拉一次详情)
    const detailBtn = [...document.body.querySelectorAll('button')].find((btn) =>
      (btn.textContent ?? '').trim().includes('详情'),
    )!
    detailBtn.click()
    await vi.waitFor(() =>
      expect(vi.mocked(getDeployment).mock.calls.length).toBeGreaterThanOrEqual(2),
    )
    await nextTick()

    // 抽屉内停止按钮(与头部同名,取渲染顺序最后一个:抽屉 teleport 挂 body 末尾)
    const stopBtn = [...document.body.querySelectorAll('button')]
      .filter((btn) => btn.textContent?.includes('停止'))
      .pop()!
    stopBtn.click()
    await vi.waitFor(() => expect(stopDeployment).toHaveBeenCalledWith('d1'))
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已停止:prod-deploy'))

    // 外部拓扑视图立即联动重刷:停止成功后有两笔新增详情请求——
    // 抽屉自身 loadDetail + 视图经 notify→subscribe→load(running 态轮询为 10s,不在此刻触发)
    await vi.waitFor(() =>
      expect(vi.mocked(getDeployment).mock.calls.length).toBeGreaterThanOrEqual(4),
    )
    wrapper.unmount()
  })
})

describe('DeployDetailView 轮询调度', () => {
  it('creating 3s 一轮;进入终态后降频为 10s', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(getDeployment).mockResolvedValue(
        makeDetail({ deployment: makeRecord({ status: 'creating', phase: 'PULL_IMAGE' }) }),
      )
      const wrapper = mountView(makeRecord({ status: 'creating', phase: 'PULL_IMAGE' }))
      await vi.advanceTimersByTimeAsync(0)
      expect(getDeployment).toHaveBeenCalledTimes(1)

      // creating:3s 一轮
      await vi.advanceTimersByTimeAsync(3000)
      expect(getDeployment).toHaveBeenCalledTimes(2)

      // 下一轮起返回终态 running:间隔降频为 10s
      vi.mocked(getDeployment).mockResolvedValue(makeDetail())
      await vi.advanceTimersByTimeAsync(3000)
      expect(getDeployment).toHaveBeenCalledTimes(3)
      await vi.advanceTimersByTimeAsync(3000)
      expect(getDeployment).toHaveBeenCalledTimes(3)
      await vi.advanceTimersByTimeAsync(7000)
      expect(getDeployment).toHaveBeenCalledTimes(4)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('卸载后停止轮询', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountView(makeRecord({ status: 'creating' }))
      await vi.advanceTimersByTimeAsync(0)
      expect(getDeployment).toHaveBeenCalledTimes(1)
      wrapper.unmount()
      await vi.advanceTimersByTimeAsync(10000)
      expect(getDeployment).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
