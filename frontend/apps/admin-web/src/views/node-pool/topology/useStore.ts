/**
 * 工作流拓扑视图:类型定义与接口数据适配。
 * 数据来源于 /api/v1/admin/clusters/topology,在此适配为 buildGraph 消费的视图模型。
 */
import type { ClusterStatus, ClusterType } from '@/api/cluster'
import type { GpuStatus } from '@/api/gpu'
import type { NodeStatus } from '@/api/node'
import { getAllTopologies, type ClusterTopology } from '@/api/topology'

export type { ClusterStatus, ClusterType, GpuStatus, NodeStatus }
export type DeployStatus = 'running' | 'deploying' | 'stopped'

export interface FlowCluster {
  id: number
  name: string
  type: ClusterType
  status: ClusterStatus
}

export interface FlowNode {
  /** 节点 UUID */
  id: string
  clusterId: number
  hostname: string
  ip: string
  status: NodeStatus
  /** 总内存 GB(缓冲缺失回退 DB) */
  memoryGb: number
  /** 已用内存 GB(实时,离线为 0) */
  memoryUsedGb: number
  /** 内存使用率 0-100(实时,离线为 0,展示时结合 status 区分离线/空闲) */
  memoryPercent: number
  /** CPU 使用率 0-100(实时,离线为 0) */
  cpuPercent: number
}

export interface FlowGpu {
  id: number
  /** 硬件 UUID,部署占用按此关联 */
  uuid: string
  nodeId: string
  /** 型号,如 RTX 4090 */
  name: string
  /** 显存总量 GB */
  memory: number
  /** 已用显存 GB(实时) */
  usedVramGb: number
  /** 可用显存 GB(可空) */
  availVramGb: number | null
  /** 利用率百分比 0-100 */
  util: number
  status: GpuStatus
}

export interface FlowEngine {
  /** 引擎标识,如 vllm(部署按此关联) */
  key: string
  name: string
  /** 取自镜像 tag,解析不到为 '--' */
  version: string
}

export interface FlowModel {
  id: number
  name: string
  /** 模型类别,如 llm */
  category: string
  format: string | null
  /** 权重大小 GB */
  sizeGb: number | null
}

export interface FlowReplicaSummary {
  /** 副本序号(人读从 1 起,展示 r{seq}) */
  seq: number
  /** provisioning | serving | down */
  status: string
  /** 落点节点 UUID */
  nodeIds: string[]
}

export interface FlowDeploy {
  /** 部署 UUID */
  id: string
  clusterId: number
  name: string
  status: DeployStatus
  /** 占用的 GPU uuid 列表 */
  gpuUuids: string[]
  engineKey: string
  modelId: number
  replicas: number
  /** 推理服务访问地址(可空) */
  endpoint: string | null
  /** 集群拓扑实时探测的健康态(healthy/degraded/down);无数据为 null */
  healthStatus: string | null
  /** 轻量副本摘要(引擎明细在部署详情接口);inspect 预览为空列表 */
  replicaSummaries: FlowReplicaSummary[]
}

/** 图节点 data.record 负载:原始记录 + 聚合字段 */
export type ClusterNodeData = FlowCluster & { nodeCount: number; gpuTotal: number }
export type NodeNodeData = FlowNode & {
  gpus: FlowGpu[]
  deployCount: number
  /** 关联部署名列表(详情面板展示) */
  deployNames: string[]
  /** 承载副本引用(r{seq} · 部署名;由部署副本摘要按落点节点反查) */
  replicaRefs: string[]
}
export type GpuNodeData = FlowGpu & { nodeHost: string }
export type DeployNodeData = FlowDeploy & { engineName: string; modelName: string }
export type EngineNodeData = FlowEngine & { deployCount: number }
export interface AggregateNodeData {
  nodeCount: number
  gpuTotal: number
  onlineNodes: number
}

/** 单集群完整图数据 */
export interface ClusterGraphData {
  cluster: FlowCluster
  nodes: FlowNode[]
  gpus: FlowGpu[]
  deploys: FlowDeploy[]
  engines: FlowEngine[]
  models: FlowModel[]
}

/** 状态文案与状态点颜色(UnoCSS 语义色类) */
export const NODE_STATUS_MAP: Record<NodeStatus, { label: string; dot: string }> = {
  online: { label: '在线', dot: 'bg-success' },
  offline: { label: '离线', dot: 'bg-error' },
  maintenance: { label: '维护', dot: 'bg-warning' },
  busy: { label: '繁忙', dot: 'bg-info' },
  drained: { label: '已排空', dot: 'bg-secondary' },
}
export const GPU_STATUS_MAP: Record<GpuStatus, { label: string; dot: string }> = {
  online: { label: '在线', dot: 'bg-success' },
  stale: { label: '心跳过期', dot: 'bg-warning' },
  offline: { label: '离线', dot: 'bg-error' },
}
export const DEPLOY_STATUS_MAP: Record<DeployStatus, { label: string; dot: string }> = {
  running: { label: '运行中', dot: 'bg-success' },
  deploying: { label: '部署中', dot: 'bg-warning' },
  stopped: { label: '已停止', dot: 'bg-error' },
}

/** 部署/端点健康态 → 文案 + 状态点(与部署详情视图同口径) */
export const DEPLOY_HEALTH_MAP: Record<string, { label: string; dot: string }> = {
  healthy: { label: '健康', dot: 'bg-success' },
  degraded: { label: '降级', dot: 'bg-warning' },
  down: { label: '异常', dot: 'bg-error' },
}

/** 从镜像名解析版本 tag(registry 带端口但无 tag 时归 '--') */
function parseEngineVersion(image: string): string {
  const lastColon = image.lastIndexOf(':')
  if (lastColon === -1 || lastColon < image.lastIndexOf('/')) return '--'
  return image.slice(lastColon + 1) || '--'
}

/** 接口数据 → 图数据;引擎/模型只保留被本集群部署引用到的 */
function adaptTopology(raw: ClusterTopology): ClusterGraphData {
  const deploys: FlowDeploy[] = raw.deployments.map((item) => ({
    id: item.deployment_id,
    clusterId: item.cluster_id,
    name: item.name,
    status: item.status as DeployStatus,
    gpuUuids: item.gpu_uuids,
    engineKey: item.engine,
    modelId: item.model_id,
    replicas: item.replicas,
    endpoint: item.api_endpoint,
    healthStatus: item.health_status,
    replicaSummaries: (item.replica_summaries ?? []).map((summary) => ({
      seq: summary.seq,
      status: summary.status,
      nodeIds: summary.node_ids,
    })),
  }))
  return {
    cluster: { ...raw.cluster },
    nodes: raw.nodes.map((item) => ({
      id: item.id,
      clusterId: item.cluster_id,
      hostname: item.hostname,
      ip: item.ip,
      status: item.status,
      memoryGb: item.memory_gb,
      memoryUsedGb: item.memory_used_gb,
      memoryPercent: item.memory_percent,
      cpuPercent: item.cpu_percent,
    })),
    gpus: raw.gpus.map((item) => ({
      id: item.id,
      uuid: item.uuid,
      nodeId: item.node_id,
      name: item.name,
      memory: item.total_vram_gb,
      usedVramGb: item.used_vram_gb,
      availVramGb: item.available_vram_gb,
      util: Math.round(item.util ?? 0),
      status: item.status,
    })),
    deploys,
    engines: raw.engines
      .filter((item) => deploys.some((deploy) => deploy.engineKey === item.engine))
      .map((item) => ({
        key: item.engine,
        name: item.display_name,
        version: parseEngineVersion(item.image),
      })),
    models: raw.models
      .filter((item) => deploys.some((deploy) => deploy.modelId === item.id))
      .map((item) => ({
        id: item.id,
        name: item.name,
        category: item.model_type,
        format: item.format,
        sizeGb: item.size_gb,
      })),
  }
}

/** 拓扑轮询间隔(ms):数据含实时利用率/状态与部署变化,10s 平衡实时性与后端压力 */
export const POLL_INTERVAL_MS = 10_000

export function useStore() {
  /** 拉取全部 active 集群拓扑并适配为图数据(接口异常由 HTTP 层统一提示) */
  const fetchTopologies = async (signal?: AbortSignal): Promise<ClusterGraphData[]> => {
    const list = await getAllTopologies(signal)
    return (list ?? []).map(adaptTopology)
  }

  return { fetchTopologies }
}
