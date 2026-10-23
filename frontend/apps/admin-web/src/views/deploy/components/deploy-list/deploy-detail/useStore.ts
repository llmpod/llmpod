import {
  getDeployment,
  getDeploymentNodeSummary,
  type DeployNodeSummaryItem,
  type DeploymentDetailOut,
  type DeploymentInstance,
  type InstanceStatus,
} from '@/api/deployment'
import { getClusterTopology, type ClusterTopology, type TopologyGpu, type TopologyNode } from '@/api/topology'
import { formatDateTime } from '../useStore'

/** n-tag 配色类型(与本视图用到的取值对齐) */
export type TagType = 'default' | 'info' | 'success' | 'warning' | 'error'

/** tag 配色 → 状态点颜色类(UnoCSS 语义色,与 node-pool/topology 图例口径一致) */
export function tagTypeDot(type: TagType): string {
  const map: Record<TagType, string> = {
    info: 'bg-info',
    success: 'bg-success',
    warning: 'bg-warning',
    error: 'bg-error',
    default: 'bg-secondary',
  }
  return map[type]
}

/** 实例状态 → 标签 + 状态点(对应后端 Instance.status 大写枚举:实例态 ≠ 部署 status;CRASHED = 容器崩溃待重试) */
export const INSTANCE_STATUS_MAP: Record<InstanceStatus, { label: string; type: TagType; dot: string }> = {
  PENDING: { label: '排队中', type: 'default', dot: 'bg-secondary' },
  STARTING: { label: '启动中', type: 'info', dot: 'bg-info' },
  RUNNING: { label: '运行中', type: 'info', dot: 'bg-info' },
  READY: { label: '就绪', type: 'success', dot: 'bg-success' },
  CRASHED: { label: '已崩溃', type: 'error', dot: 'bg-error' },
  FAILED: { label: '失败', type: 'error', dot: 'bg-error' },
  STOPPING: { label: '停止中', type: 'warning', dot: 'bg-warning' },
  STOPPED: { label: '已停止', type: 'default', dot: 'bg-secondary' },
}

/** 节点步骤状态 → 徽标配色(logs/nodes 的步骤态,小写,≠ 部署 status,别混) */
export const NODE_STEP_STATUS_TYPE: Record<string, TagType> = {
  pending: 'default',
  running: 'info',
  success: 'success',
  failed: 'error',
  cancelled: 'warning',
}

/** 节点步骤 → 文案(对接文档 §4 step 词表 + 建议标题;wait_ready 已废弃,仅历史数据兜底) */
export const NODE_STEP_LABEL: Record<string, string> = {
  queued: '排队中',
  submit: '提交',
  staging_model: '下载模型',
  pull_image: '拉取镜像',
  start_head: '启动主节点',
  head_health_check: '主节点健康检查',
  start_workers: '启动工作节点',
  worker_health_check: '工作节点健康检查',
  finalize: '生成端点',
  running: '就绪',
  start: '启动',
  stop: '停止',
  stopping: '停止中',
  start_instance: '启动实例',
  orchestration: '编排',
  scale: '扩缩容',
  wait_ready: '等待就绪',
}

/** 节点步骤进度状态 → 文案(logs/nodes 的步骤态;卡片徽标用 type 配色,详情面板分行展示用 label) */
export const NODE_STEP_STATUS_LABEL: Record<string, string> = {
  pending: '等待中',
  running: '进行中',
  success: '已完成',
  failed: '已失败',
  cancelled: '已取消',
}

/** 节点徽标视图模型:步骤文案 + 步骤态配色 + 悬浮提示(step + message + 时间);message/updatedAt 供详情面板分行展示 */
export interface NodeBadgeView {
  label: string
  type: TagType
  tip: string
  /** 节点主机名(logs/nodes 摘要,可读名优先于 node_id 展示);无摘要为 null */
  hostname: string | null
  /** 步骤进度文案(结构化;tip 为拼接串) */
  statusLabel: string
  message: string | null
  /** 最后一条日志时间(已格式化) */
  updatedAt: string
}

/** 组装节点徽标:label 取步骤文案,tip 拼步骤态/消息/时间;step 为空显示「未知」 */
export function buildNodeBadge(item: DeployNodeSummaryItem): NodeBadgeView {
  const step = item.step ?? ''
  const status = item.status ?? 'pending'
  return {
    label: NODE_STEP_LABEL[step] ?? (step || '未知'),
    type: NODE_STEP_STATUS_TYPE[status] ?? 'default',
    hostname: item.node_hostname ?? null,
    tip: [
      `状态 ${status}`,
      item.message,
      item.updated_at ? formatDateTime(item.updated_at) : null,
    ]
      .filter(Boolean)
      .join(' · '),
    statusLabel: NODE_STEP_STATUS_LABEL[status] ?? status,
    message: item.message,
    updatedAt: item.updated_at ? formatDateTime(item.updated_at) : '—',
  }
}

/** 拓扑副本分组:head(组内最小 rank) + workers;rank===0 必为 head,多机时 workers rank 连续 */
export interface ReplicaGroup {
  index: number
  head: DeploymentInstance | null
  workers: DeploymentInstance[]
}

/**
 * 实例按副本分组:按 rank 升序后均分为 replicas 组(每组实例数 = ceil(总数/副本数))。
 * 单副本多机(nnodes>1)为一组 head + headless workers;多副本时逐副本展开。
 */
export function groupReplicas(
  instances: DeploymentInstance[],
  replicas: number,
): ReplicaGroup[] {
  if (!instances.length) return []
  const sorted = [...instances].sort((a, b) => a.rank - b.rank)
  const replicaCount = Math.max(1, Math.trunc(replicas) || 1)
  const perReplica = Math.max(1, Math.ceil(sorted.length / replicaCount))
  const groups: ReplicaGroup[] = []
  for (let index = 0; index * perReplica < sorted.length; index += 1) {
    const chunk = sorted.slice(index * perReplica, (index + 1) * perReplica)
    groups.push({ index, head: chunk[0] ?? null, workers: chunk.slice(1) })
  }
  return groups
}

/**
 * 轮询间隔:变更中的状态密一些——creating/deleting 或扩缩容子态(SCALING)3s,其余 10s。
 * SCALING 是 phase 而非 status(status 仍为 running),需单独传参判断。
 */
export function pollIntervalMs(status: string, phase?: string): number {
  return status === 'creating' || status === 'deleting' || phase === 'SCALING' ? 3000 : 10000
}

/** 物理节点资产(详情面板展示):节点信息 + 该节点 GPU 列表,来自集群拓扑接口 */
export interface NodeAssetsView {
  /** node_id → 节点(hostname/IP/状态) */
  nodes: Map<string, TopologyNode>
  /** node_id → GPU 列表 */
  gpus: Map<string, TopologyGpu[]>
}

/**
 * 部署详情数据层:详情(拓扑骨架) + 每节点步骤摘要(徽标动态层) + 集群拓扑(GPU 列)。
 * 节点摘要失败不阻塞详情(徽标置空);详情失败保留上一次数据,由 HTTP 层统一提示。
 * 集群拓扑随轮询刷新(GPU 利用率/显存是实时值);失败保留旧值不阻塞。
 * 节点资产(主机名/IP/GPU)按需加载:点击物理节点时拉取,面板打开期间随轮询刷新利用率。
 */
export function useStore(deploymentId: string) {
  const detail = shallowRef<DeploymentDetailOut | null>(null)
  const nodeSummary = shallowRef<DeployNodeSummaryItem[]>([])
  /** 集群拓扑(GPU 列数据源;失败为 null 保留旧值) */
  const topology = shallowRef<ClusterTopology | null>(null)
  const loading = ref(false)
  const nodeAssets = shallowRef<NodeAssetsView | null>(null)
  const assetsLoading = ref(false)

  async function load(): Promise<void> {
    loading.value = true
    try {
      const [detailResult, nodesResult] = await Promise.all([
        getDeployment(deploymentId),
        getDeploymentNodeSummary(deploymentId).catch(() => [] as DeployNodeSummaryItem[]),
      ])
      detail.value = detailResult
      nodeSummary.value = nodesResult
      // GPU 列数据源:集群拓扑(异步回填,不拖慢轮询;失败静默保留旧值)
      const clusterId = detailResult.deployment.cluster_id
      if (clusterId) {
        void getClusterTopology(clusterId)
          .then((topo) => {
            topology.value = topo
          })
          .catch(() => {})
      }
    } catch {
      // 详情接口异常已由 HTTP 层统一提示,保留空态/上一次数据
    } finally {
      loading.value = false
    }
  }

  /** 拉取集群拓扑资产;失败保留上一次数据(HTTP 层已提示) */
  async function loadNodeAssets(clusterId: number): Promise<void> {
    assetsLoading.value = true
    try {
      const topology = await getClusterTopology(clusterId).catch(() => null)
      if (!topology) return
      const gpus = new Map<string, TopologyGpu[]>()
      for (const gpu of topology.gpus) {
        const list = gpus.get(gpu.node_id) ?? []
        list.push(gpu)
        gpus.set(gpu.node_id, list)
      }
      nodeAssets.value = {
        nodes: new Map(topology.nodes.map((node) => [node.id, node])),
        gpus,
      }
    } finally {
      assetsLoading.value = false
    }
  }

  return {
    detail,
    nodeSummary,
    topology,
    loading,
    load,
    nodeAssets,
    assetsLoading,
    loadNodeAssets,
  }
}
