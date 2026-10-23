import type { Edge, Node } from '@vue-flow/core'
import {
  normalizedTopology,
  toReplicaSeq,
  type DeployNodeSummaryItem,
  type DeploymentEngineInstance,
  type DeploymentInstance,
  type DeploymentReplica,
  type ModelDeploymentOut,
} from '@/api/deployment'
import type { ClusterTopology, TopologyGpu } from '@/api/topology'
import { DEPLOY_PHASE_LABEL, deployStatusMap, formatDateTime } from '../useStore'
import {
  buildNodeBadge,
  groupReplicas,
  INSTANCE_STATUS_MAP,
  tagTypeDot,
  type NodeBadgeView,
} from './useStore'

/**
 * 部署详情拓扑图装配(纯函数):列布局与 node-pool/topology graph.ts 同算法。
 * 列:部署 → 副本(三态徽标) → 物理节点(徽标来自 logs/nodes 摘要) → head 分片 → worker 分片(可缺省) → GPU(该部署占用,来自集群拓扑)。
 * 连线:部署 → 副本(推理链路入口,serving 份才流动,部署级 running 总门控);副本 → 物理节点(静态,跨机份连多节点);
 * 物理节点 → 分片(head 为链路入口按份流动)/worker(静态);head → worker(推理链路);物理节点 → GPU(静态)。
 */

/** 部署拓扑属性(详情面板分行展示;卡片不再展示) */
export interface DeploymentTopologyView {
  tp: number
  pp: number
  replicas: number
  nnodes: number
  /** 上下文长度;未设置为 null */
  maxModelLen: number | null
}

/** 部署图节点 data.record 负载 */
export interface DeploymentNodeData {
  name: string
  modelName: string
  engine: string
  statusLabel: string
  statusDot: string
  /** creating 期间的编排子态文案;其余为 null */
  phaseLabel: string | null
  /** 集群拓扑实时探测的健康态(healthy/degraded/down);无数据为 null */
  healthStatus: string | null
  /** 模型拓扑属性;无拓扑信息为 null */
  topology: DeploymentTopologyView | null
  /** 创建/更新时间(已格式化) */
  createdAt: string
  updatedAt: string
}

/** 部署/端点健康态 → 文案 + 语义色(文本色/状态点) */
export const HEALTH_STATUS_TEXT: Record<string, { label: string; text: string; dot: string }> = {
  healthy: { label: '健康', text: 'text-success', dot: 'bg-success' },
  degraded: { label: '降级', text: 'text-warning', dot: 'bg-warning' },
  down: { label: '异常', text: 'text-error', dot: 'bg-error' },
}

/** 副本三态(详情 replicas 层权威值;层缺失时由分片实例态派生);后端 status 为自由 string,未知值走兜底 */
export type ReplicaStatus = string

/** 副本三态 → 徽标文案 + 状态点(provisioning 灰 + 脉动,见 ReplicaNode);未知值兜底原样展示(文档 §8-5 承诺不崩) */
export const REPLICA_STATUS_MAP: Record<string, { label: string; dot: string }> = {
  serving: { label: '服务中', dot: 'bg-success' },
  down: { label: '异常', dot: 'bg-error' },
  provisioning: { label: '供给中', dot: 'bg-secondary' },
}

/** 副本卡片引擎摘要行(v1 每份单引擎,多角色时逐行) */
export interface ReplicaEngineView {
  role: string
  /** 并行规模,如 tp=2 · pp=1 */
  parallel: string
  /** GPU 架构(异构份各不相同;缺省为 null) */
  gpuArch: string | null
  status: string
}

/** 副本图节点 data.record 负载 */
export interface ReplicaNodeData {
  /** 副本 ID;replicas 层缺失时前端推导的份为 null */
  replicaId: string | null
  /** 副本序号(人读 1 基;权威路径 = 后端 seq+1,推导路径 = index+1,两条路径已对齐) */
  seq: number
  status: ReplicaStatus
  statusLabel: string
  statusDot: string
  /** 供给中脉动门控:仅部署 running 时脉动(stopped/failed 部署的份不播进行中动画) */
  pulse: boolean
  /** 引擎摘要(副标题与详情面板共用) */
  engines: ReplicaEngineView[]
  /** 份内容器分片数 */
  shardCount: number
  /** 份内端点数(每副本一条) */
  endpointCount: number
  /** replicas 层缺失时按实例分布推导 */
  derived: boolean
}

/** 实例图节点 data.record 负载 */
export interface InstanceNodeData {
  /** 实例 ID(日志弹窗等入口用) */
  instanceId: string
  rank: number
  role: string
  head: boolean
  /** 所属副本序号(人读从 1 起) */
  replicaSeq: number
  replicaCount: number
  statusLabel: string
  statusDot: string
  nodeId: string | null
  endpoint: string | null
  /** 就绪时间(已格式化;未就绪为 —) */
  readyAt: string
  lastError: string | null
  /** 以下为详情面板字段(卡片不展示) */
  containerId: string | null
  /** 启动/停止时间(已格式化;未发生为 —) */
  startedAt: string
  stoppedAt: string
}

/** 物理节点承载实例引用(详情面板展示) */
export interface PhyInstanceRef {
  /** 如 `#0 full · r1 · Head` */
  label: string
  statusLabel: string
}

/** 物理节点图节点 data.record 负载 */
export interface PhyNodeData {
  nodeId: string
  /** 节点步骤徽标(logs/nodes 摘要);无摘要为 null */
  badge: NodeBadgeView | null
  statusDot: string
  /** 承载实例列表(按 assigned_node 汇总) */
  instances: PhyInstanceRef[]
}

/** GPU 图节点 data.record 负载:集群拓扑原始记录(实时利用率/显存,随轮询刷新) */
export type GpuNodeData = TopologyGpu

export interface BuildDeployGraphInput {
  deployment: ModelDeploymentOut
  instances: DeploymentInstance[]
  nodeSummary: DeployNodeSummaryItem[]
  /** 模型名回显(列表 store 的 id → 名称映射,未命中为 #id) */
  modelName: string
  /** 集群拓扑(GPU 列数据源;null/加载失败则不渲染 GPU 列) */
  topology: ClusterTopology | null
  /** 副本层组图数据(详情 replicas;缺层回退 instances 均分启发式) */
  replicas?: DeploymentReplica[]
}

export interface BuiltDeployGraph {
  nodes: Node[]
  edges: Edge[]
}

/** 画布节点类型 */
export type DeployNodeType = 'deployment' | 'replica' | 'instance' | 'phy' | 'gpu'

/** 详情面板选中态:节点类型 + record(由 controller 按选中节点 id 实时取,轮询自动刷新) */
export interface PanelSelection {
  type: DeployNodeType
  record: DeploymentNodeData | ReplicaNodeData | InstanceNodeData | PhyNodeData | GpuNodeData
}

/** 日志按钮入口(各层节点卡片触发):部署→编排时间线;实例→容器 stdout;物理节点→按节点过滤的时间线 */
export type LogEntry =
  { type: 'timeline'; nodeId?: string } | { type: 'console'; instanceId: string }

/** 列 x 坐标(flow 坐标系);副本列插在部署与物理节点之间,head/worker 分片列顺延,GPU 列在分片之后(与 worker 列留 100px,同 head→worker 间距) */
const COLUMN_X = { deployment: 0, replica: 300, phy: 620, head: 940, worker: 1220, gpu: 1500 }
const ROW_GAP = 160

/** 列内纵向均布并整体垂直居中(与 node-pool/topology graph.ts 同算法) */
function columnY(index: number, total: number): number {
  return (index - (total - 1) / 2) * ROW_GAP
}

/** 静态结构线 / 推理链路线(颜色由 index.vue 的 .edge-static/.edge-flow 样式控制) */
function edge(source: string, target: string, animated: boolean): Edge {
  return {
    id: `e-${source}-${target}`,
    source,
    target,
    animated,
    class: animated ? 'edge-flow' : 'edge-static',
  }
}

function instanceNodeData(
  instance: DeploymentInstance,
  head: boolean,
  replicaSeq: number,
  replicaCount: number,
): InstanceNodeData {
  const conf = INSTANCE_STATUS_MAP[instance.status] ?? {
    label: instance.status,
    dot: 'bg-secondary',
  }
  return {
    instanceId: instance.instance_id,
    rank: instance.rank,
    role: instance.role,
    head,
    replicaSeq,
    replicaCount,
    statusLabel: conf.label,
    statusDot: conf.dot,
    nodeId: instance.assigned_node,
    endpoint: instance.endpoint,
    readyAt: formatDateTime(instance.ready_at),
    lastError: instance.last_error,
    containerId: instance.container_id,
    startedAt: formatDateTime(instance.started_at),
    stoppedAt: formatDateTime(instance.stopped_at),
  }
}

/** 副本视图行(图装配中间形态):replicas 层权威,层缺失按 rank 均分推导;seq 统一为 1 基展示序号 */
export interface ReplicaRow {
  replicaId: string | null
  seq: number
  status: ReplicaStatus
  /** 引擎明细(replicas 层带来;推导份为空) */
  engines: DeploymentEngineInstance[]
  /** 份内容器分片(按 shard_ids 反查;推导份为均分 chunk) */
  members: DeploymentInstance[]
  /** head = 份内最小 rank 分片(推理链路入口);空份为 null */
  head: DeploymentInstance | null
  /** 份内端点数 */
  endpointCount: number
  /** replicas 层缺失时前端推导 */
  derived: boolean
}

/** 分片实例态派生副本态(缺 replicas 层时):任一分片终态失败(CRASHED/FAILED/STOPPED) → down;全部就绪 → serving;其余 provisioning */
function deriveReplicaStatus(members: DeploymentInstance[]): ReplicaStatus {
  if (members.some((item) => item.status === 'FAILED' || item.status === 'CRASHED' || item.status === 'STOPPED'))
    return 'down'
  if (members.length > 0 && members.every((item) => item.status === 'READY')) return 'serving'
  return 'provisioning'
}

/**
 * 实例 → 副本视图行:
 * 有 replicas 层时按 engines[].shard_ids 反查归属(权威),未被覆盖的实例(数据不一致)
 * 合成附加兜底份保持画布不丢分片;无 replicas 层回退 groupReplicas 均分启发式。
 */
export function buildReplicaRows(
  instances: DeploymentInstance[],
  replicas: DeploymentReplica[] | null | undefined,
  fallbackCopies: number,
): ReplicaRow[] {
  if (!replicas?.length) {
    return groupReplicas(instances, fallbackCopies).map((group, index) => {
      const members = [...(group.head ? [group.head] : []), ...group.workers]
      return {
        replicaId: null,
        seq: index + 1,
        status: deriveReplicaStatus(members),
        engines: [],
        members,
        head: group.head,
        endpointCount: 0,
        derived: true,
      }
    })
  }
  const rows: ReplicaRow[] = replicas.map((replica) => {
    const shardIds = new Set(
      (replica.engines ?? []).flatMap((engine) => engine.shard_ids ?? []),
    )
    const members = instances
      .filter((item) => shardIds.has(item.instance_id))
      .sort((a, b) => a.rank - b.rank)
    return {
      replicaId: replica.replica_id,
      seq: toReplicaSeq(replica.seq),
      status: replica.status,
      engines: replica.engines ?? [],
      members,
      head: members[0] ?? null,
      endpointCount: (replica.endpoint_ids ?? []).length,
      derived: false,
    }
  })
  // 兜底:未被任何份 shard_ids 覆盖的实例按均分合成附加份(序号顺延,标记 derived)
  const covered = new Set(rows.flatMap((row) => row.members.map((item) => item.instance_id)))
  const leftovers = instances.filter((item) => !covered.has(item.instance_id))
  if (leftovers.length) {
    const nextSeq = rows.reduce((max, row) => Math.max(max, row.seq), 0)
    groupReplicas(leftovers, 1).forEach((group, index) => {
      const members = [...(group.head ? [group.head] : []), ...group.workers]
      rows.push({
        replicaId: null,
        seq: nextSeq + index + 1,
        status: deriveReplicaStatus(members),
        engines: [],
        members,
        head: group.head,
        endpointCount: 0,
        derived: true,
      })
    })
  }
  return rows.sort((a, b) => a.seq - b.seq)
}

/** 由部署详情 + 节点摘要组装 Vue Flow 节点与连线(纯函数) */
export function buildDeployGraph(input: BuildDeployGraphInput): BuiltDeployGraph {
  const { deployment, instances, nodeSummary, modelName, topology, replicas } = input
  const flowNodes: Node[] = []
  const flowEdges: Edge[] = []
  /** 推理链路动画总门控:仅部署 running 时流动(与 node-pool 口径一致) */
  const running = deployment.status === 'running'

  // 部署列(launching 等历史值按 creating 渲染;未知值原样展示)
  const rawStatus = deployment.status as string
  const statusKey = rawStatus === 'launching' ? 'creating' : rawStatus
  const statusConf = deployStatusMap[statusKey as keyof typeof deployStatusMap] ?? {
    label: rawStatus,
    type: 'default' as const,
  }
  const topo = deployment.spec?.topology
  // units×copies 与旧四数双写兼容:归一后统一取数;两写法都缺才是无拓扑
  const normalized =
    topo && (topo.units?.length || topo.tp) ? normalizedTopology(topo) : null
  const deployData: DeploymentNodeData = {
    name: deployment.name,
    modelName,
    engine: deployment.engine,
    statusLabel: statusConf.label,
    statusDot: tagTypeDot(statusConf.type),
    phaseLabel:
      statusKey === 'creating' ? (DEPLOY_PHASE_LABEL[deployment.phase] ?? deployment.phase) : null,
    healthStatus:
      topology?.deployments.find((item) => item.deployment_id === deployment.deployment_id)
        ?.health_status ?? null,
    topology: normalized
      ? {
          tp: normalized.units[0].tp,
          pp: normalized.units[0].pp,
          replicas: normalized.copies,
          nnodes: normalized.units[0].nnodes,
          maxModelLen: normalized.maxModelLen ?? null,
        }
      : null,
    createdAt: formatDateTime(deployment.created_at),
    updatedAt: formatDateTime(deployment.updated_at),
  }
  const deployNodeId = `deployment-${deployment.deployment_id}`
  flowNodes.push({
    id: deployNodeId,
    type: 'deployment',
    position: { x: COLUMN_X.deployment, y: 0 },
    data: { record: deployData },
  })

  // 副本视图行:replicas 层(投影链权威)优先,shard_ids 反查分片归属;层缺失回退均分启发式
  const replicaRows = buildReplicaRows(instances, replicas, normalized?.copies ?? topo?.replicas ?? 1)
  const replicaCount = replicaRows.length
  /** 分片实例 → 所属份(物理节点承载引用标注用) */
  const rowOfInstance = new Map<string, ReplicaRow>()
  for (const row of replicaRows) {
    for (const item of row.members) rowOfInstance.set(item.instance_id, row)
  }
  /** 份级动画:部署 running 且该份 serving 才流动 */
  const flowing = (row: ReplicaRow) => running && row.status === 'serving'

  // 副本列(部署与物理节点之间):三态徽标,provisioning 灰 + 脉动(样式见 ReplicaNode)
  replicaRows.forEach((row, index) => {
    // 未知副本状态兜底原样展示(文档 §8-5:不崩);已知值走三态映射
    const conf = REPLICA_STATUS_MAP[row.status] ?? { label: row.status, dot: 'bg-secondary' }
    const record: ReplicaNodeData = {
      replicaId: row.replicaId,
      seq: row.seq,
      status: row.status,
      statusLabel: conf.label,
      statusDot: conf.dot,
      // 脉动 = 供给中且部署运行中;终态部署(stopped/failed)的份只静态展示
      pulse: running && row.status === 'provisioning',
      engines: row.engines.map((engine) => ({
        role: engine.role,
        parallel: engine.pp > 1 ? `tp=${engine.tp} · pp=${engine.pp}` : `tp=${engine.tp}`,
        gpuArch: engine.gpu_arch,
        status: engine.status,
      })),
      shardCount: row.members.length,
      endpointCount: row.endpointCount,
      derived: row.derived,
    }
    // 节点 id 用实体键(replica_id):层缺失↔恢复切换时 id 稳定,详情面板不漂移;推导份用展示序号
    const replicaNodeId = row.replicaId ? `replica-${row.replicaId}` : `replica-derived-${row.seq}`
    flowNodes.push({
      id: replicaNodeId,
      type: 'replica',
      position: { x: COLUMN_X.replica, y: columnY(index, replicaRows.length) },
      data: { record },
    })
    // 部署 → 副本:推理链路入口,仅 serving 份流动
    flowEdges.push(edge(deployNodeId, replicaNodeId, flowing(row)))
  })

  // 分片列:逐份展开 head(份内最小 rank)+ workers
  /** head 分片实例 id 集合(phy 承载引用按此标 Head/Worker) */
  const headIds = new Set(
    replicaRows.flatMap((row) => (row.head ? [row.head.instance_id] : [])),
  )
  const headEntries = replicaRows
    .map((row) => ({ instance: row.head, row }))
    .filter((item): item is { instance: DeploymentInstance; row: ReplicaRow } =>
      Boolean(item.instance),
    )
  const workerEntries = replicaRows.flatMap((row) =>
    row.members.slice(1).map((instance) => ({ instance, row })),
  )
  headEntries.forEach(({ instance, row }, index) => {
    flowNodes.push({
      id: `inst-${instance.instance_id}`,
      type: 'instance',
      position: { x: COLUMN_X.head, y: columnY(index, headEntries.length) },
      data: { record: instanceNodeData(instance, true, row.seq, replicaCount) },
    })
  })
  workerEntries.forEach(({ instance, row }, index) => {
    const id = `inst-${instance.instance_id}`
    flowNodes.push({
      id,
      type: 'instance',
      position: { x: COLUMN_X.worker, y: columnY(index, workerEntries.length) },
      data: { record: instanceNodeData(instance, false, row.seq, replicaCount) },
    })
    // head → workers 连线(同份内 rank 连续,最小 rank 为 head)
    if (row.head) flowEdges.push(edge(`inst-${row.head.instance_id}`, id, flowing(row)))
  })

  // 物理节点列:去重 assigned_node,徽标按 node_id 对齐 logs/nodes 摘要;
  // 按 nodeId 稳定排序——y 落位依赖数组下标,实例列表返回顺序逐轮抖动会让相同节点跳行(轮询打断浏览),
  // 排序后同集合同落位,只有实体增减才移动
  const summaryByNode = new Map<string, DeployNodeSummaryItem>()
  for (const item of nodeSummary) {
    if (item.node_id) summaryByNode.set(item.node_id, item)
  }
  const phyIds = [
    ...new Set(instances.map((item) => item.assigned_node).filter((id): id is string => !!id)),
  ].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
  phyIds.forEach((nodeId, index) => {
    const summary = summaryByNode.get(nodeId)
    const badge = summary ? buildNodeBadge(summary) : null
    const phyData: PhyNodeData = {
      nodeId,
      badge,
      statusDot: badge ? tagTypeDot(badge.type) : 'bg-secondary',
      // 承载实例:该节点上的 head/worker 分片引用(详情面板展示,带所属份序号)
      instances: instances
        .filter((item) => item.assigned_node === nodeId)
        .map((item) => ({
          label: `#${item.rank} ${item.role} · 副本${
            rowOfInstance.get(item.instance_id)?.seq ?? 1
          }${headIds.has(item.instance_id) ? ' · Head' : ' · Worker'}`,
          statusLabel: INSTANCE_STATUS_MAP[item.status]?.label ?? item.status,
        })),
    }
    flowNodes.push({
      id: `phy-${nodeId}`,
      type: 'phy',
      position: { x: COLUMN_X.phy, y: columnY(index, phyIds.length) },
      data: { record: phyData },
    })
  })
  // 副本 → 物理节点(静态结构线;跨机份连多节点)
  for (const row of replicaRows) {
    const nodeIds = [
      ...new Set(row.members.map((item) => item.assigned_node).filter((id): id is string => !!id)),
    ].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
    const replicaNodeId = row.replicaId ? `replica-${row.replicaId}` : `replica-derived-${row.seq}`
    for (const nodeId of nodeIds) {
      flowEdges.push(edge(replicaNodeId, `phy-${nodeId}`, false))
    }
  }
  // 物理节点 → 承载分片:head 为推理链路入口(按份流动),worker 为静态结构线
  for (const instance of instances) {
    if (instance.assigned_node) {
      const row = rowOfInstance.get(instance.instance_id)
      flowEdges.push(
        edge(
          `phy-${instance.assigned_node}`,
          `inst-${instance.instance_id}`,
          headIds.has(instance.instance_id) && row ? flowing(row) : false,
        ),
      )
    }
  }

  // GPU 列:该部署占用的卡(topology.deployments 按 deployment_id 匹配 gpu_uuids),
  // 按 phy 列顺序分组、组内按物理 index 排序后整列均布(与 node-pool/topology 同算法);
  // 行距需大于卡高(约 110px)否则相邻卡上下重叠;topology 缺失时不渲染
  const gpuUuids = new Set(
    topology?.deployments.find((item) => item.deployment_id === deployment.deployment_id)
      ?.gpu_uuids ?? [],
  )
  const occupiedGpus = (topology?.gpus ?? []).filter((gpu) => gpuUuids.has(gpu.uuid))
  if (occupiedGpus.length) {
    const gpuByNode = new Map<string, TopologyGpu[]>()
    for (const gpu of occupiedGpus) {
      const list = gpuByNode.get(gpu.node_id) ?? []
      list.push(gpu)
      gpuByNode.set(gpu.node_id, list)
    }
    // 组内按物理 index 稳定排序:均布取 indexOf,集群拓扑返回顺序抖动会让同节点 GPU 互换行
    for (const list of gpuByNode.values()) {
      list.sort((a, b) => a.index - b.index || (a.uuid < b.uuid ? -1 : a.uuid > b.uuid ? 1 : 0))
    }
    // 按 phyIds 顺序排布,兜底节点(数据不一致,phy 列未出现)排到列尾(同样稳定排序)
    const orphanNodes = [...gpuByNode.keys()]
      .filter((nodeId) => !phyIds.includes(nodeId))
      .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
    const gpuOrder = [...phyIds, ...orphanNodes].flatMap((nodeId) => gpuByNode.get(nodeId) ?? [])
    gpuOrder.forEach((gpu, index) => {
      flowNodes.push({
        id: `gpu-${gpu.uuid}`,
        type: 'gpu',
        position: { x: COLUMN_X.gpu, y: columnY(index, gpuOrder.length) },
        data: { record: gpu },
      })
      // 物理节点 → GPU(静态结构线;仅在物理节点列中存在对应节点时连线)
      if (phyIds.includes(gpu.node_id)) {
        flowEdges.push(edge(`phy-${gpu.node_id}`, `gpu-${gpu.uuid}`, false))
      }
    })
  }

  return { nodes: flowNodes, edges: flowEdges }
}
