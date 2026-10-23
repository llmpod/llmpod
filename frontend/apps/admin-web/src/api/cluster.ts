import { request } from './request'

/** 集群类型：docker | k8s */
export type ClusterType = 'docker' | 'k8s'

/** 集群状态：active=启用 | inactive=停用 */
export type ClusterStatus = 'active' | 'inactive'

/** 集群信息（对应后端 ClusterSchema） */
export interface ClusterData {
  /** 主键ID（自增） */
  id: number
  /** 集群名称 */
  name: string
  type: ClusterType
  /** 集群描述（可空） */
  description: string | null
  status: ClusterStatus
  /** 集群规格（JSON 对象，如连接信息、版本等） */
  spec: Record<string, unknown>
  created_at: string
  updated_at: string
  /** 节点总数（派生统计字段，后端已降级，不会缺字段） */
  node_count: number
  /** 在线节点数（status=online，派生统计字段，后端已降级，不会缺字段） */
  online_node_count: number
  /** GPU总数（派生统计字段，后端已降级，不会缺字段） */
  gpu_count: number
}

/** 集群分页数据（GET /api/v1/admin/clusters） */
export interface ClusterPageData {
  items: ClusterData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 列出集群查询参数 */
export interface ListClustersParams {
  /** 集群状态，不传则列出全部 */
  status?: ClusterStatus
  /** 页码，从1开始 */
  page?: number
  /** 每页条数，最大100 */
  page_size?: number
}

/** 列出集群（GET /api/v1/admin/clusters） */
export function listClusters(
  params: ListClustersParams,
  signal?: AbortSignal,
): Promise<ClusterPageData> {
  return request({ url: '/admin/clusters', method: 'GET', params, signal })
}

/** 创建集群请求体 */
export interface CreateClusterPayload {
  name: string
  type: ClusterType
  /** 集群描述，可空 */
  description?: string
}

/** 创建集群（POST /api/v1/admin/clusters） */
export function createCluster(data: CreateClusterPayload): Promise<ClusterData> {
  return request({ url: '/admin/clusters', method: 'POST', data })
}

/** 获取集群详情（GET /api/v1/admin/clusters/{cluster_id}） */
export function getCluster(clusterId: number): Promise<ClusterData> {
  return request({ url: `/admin/clusters/${clusterId}`, method: 'GET' })
}

/** 更新集群请求体：均为可选，不传则不修改（类型创建后不可改） */
export interface UpdateClusterPayload {
  name?: string
  description?: string
  status?: ClusterStatus
}

/** 更新集群（POST /api/v1/admin/clusters/{cluster_id}/update） */
export function updateCluster(
  clusterId: number,
  data: UpdateClusterPayload,
): Promise<ClusterData> {
  return request({ url: `/admin/clusters/${clusterId}/update`, method: 'POST', data })
}

/** 删除集群（POST /api/v1/admin/clusters/{cluster_id}/delete，集群下仍有节点时会被后端拒绝） */
export function deleteCluster(clusterId: number): Promise<unknown> {
  return request({ url: `/admin/clusters/${clusterId}/delete`, method: 'POST' })
}
