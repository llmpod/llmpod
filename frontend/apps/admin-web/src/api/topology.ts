import { request } from './request'
import type { ClusterStatus, ClusterType } from './cluster'
import type { GpuStatus } from './gpu'
import type { NodeStatus } from './node'

/** 拓扑集群信息(精简 ClusterSchema) */
export interface TopologyCluster {
  id: number
  name: string
  type: ClusterType
  status: ClusterStatus
}

/** 拓扑节点(精简 NodeSchema + 实时 CPU/内存,id 为 Manager 分配的 UUID) */
export interface TopologyNode {
  id: string
  cluster_id: number
  hostname: string
  ip: string
  status: NodeStatus
  /** 总内存 GB(实时,缓冲缺失回退 DB 静态总量) */
  memory_gb: number
  /** 可用内存 GB(总 - 已用) */
  available_memory_gb: number
  /** 已用内存 GB(实时,离线为 0,请结合 status 区分离线/空闲) */
  memory_used_gb: number
  /** 内存使用率 0-100(实时,离线为 0) */
  memory_percent: number
  /** CPU 使用率 0-100(实时,离线为 0) */
  cpu_percent: number
}

/** 拓扑 GPU(精简 GpuSchema + 实时利用率/可用显存) */
export interface TopologyGpu {
  id: number
  /** GPU 硬件 UUID,部署占用按此关联 */
  uuid: string
  node_id: string
  index: number
  name: string
  arch: string
  /** 显存总量 GB */
  total_vram_gb: number
  /** 已用显存 GB(实时,离线为 0) */
  used_vram_gb: number
  /** 可用显存 GB(可空) */
  available_vram_gb: number | null
  /** 利用率百分比 0-100(可空) */
  util: number | null
  status: GpuStatus
}

/** 拓扑部署(编排侧记录,关联 id 供前端 buildGraph 连线) */
export interface TopologyDeployment {
  /** 部署 ID(编排侧 UUID) */
  deployment_id: string
  cluster_id: number
  name: string
  /** running | deploying | stopped */
  status: string
  /** 编排阶段(如 RUNNING),展示以 status 为准 */
  phase: string
  /** 引擎标识,连线匹配 TopologyEngine.engine */
  engine: string
  /** 模型 ID,连线匹配 TopologyModel.id */
  model_id: number
  /** 占用的 GPU uuid 列表,连线匹配 TopologyGpu.uuid */
  gpu_uuids: string[]
  replicas: number
  nnodes: number
  /** 推理服务访问地址(可空) */
  api_endpoint: string | null
  health_status: string | null
  instance_count: number
  /** 轻量副本摘要(引擎明细只在部署详情接口);inspect 预览合成为空列表 */
  replica_summaries?: { seq: number; status: string; node_ids: string[] }[]
}

/** 拓扑引擎(image tag 即版本;对应后端 EngineSummary) */
export interface TopologyEngine {
  /** 引擎标识,如 vllm */
  engine: string
  display_name: string
  /** 镜像,如 vllm/vllm-openai:v0.23.0 */
  image: string
  supported_categories: string[]
  /** 系统必控 flag(用户 extra_args 同名会被忽略并告警);OpenAPI 非必填 */
  protected_flags?: string[]
  /** 推荐参数 chip:{name/hint/default/choices}(后端为自由对象,Step3 参数编辑下拉用);OpenAPI 非必填 */
  param_suggestions?: Record<string, unknown>[]
}

/** 拓扑模型(精简 ModelSchema) */
export interface TopologyModel {
  id: number
  name: string
  model_type: string
  size_gb: number | null
  format: string | null
}

/** 单集群拓扑数据(GET /api/v1/admin/clusters/{cluster_id}/topology) */
export interface ClusterTopology {
  cluster: TopologyCluster
  nodes: TopologyNode[]
  gpus: TopologyGpu[]
  deployments: TopologyDeployment[]
  engines: TopologyEngine[]
  models: TopologyModel[]
}

/** 全集群拓扑(GET /api/v1/admin/clusters/topology,仅 active 集群) */
export function getAllTopologies(signal?: AbortSignal): Promise<ClusterTopology[] | null> {
  return request({ url: '/admin/clusters/topology', method: 'GET', signal })
}

/** 单集群拓扑(GET /api/v1/admin/clusters/{cluster_id}/topology) */
export function getClusterTopology(
  clusterId: number,
  signal?: AbortSignal,
): Promise<ClusterTopology> {
  return request({ url: `/admin/clusters/${clusterId}/topology`, method: 'GET', signal })
}
