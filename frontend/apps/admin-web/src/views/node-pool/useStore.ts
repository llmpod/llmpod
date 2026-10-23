import type { TableRequestContext } from '@repo/hooks'
import {
  listClusters,
  createCluster,
  updateCluster,
  deleteCluster,
  type ClusterData,
  type ClusterStatus,
  type ClusterType,
} from '@/api/cluster'
import { listNodes } from '@/api/node'
import { listGpus } from '@/api/gpu'
import { markClusterCreated, resetClusterCache } from '@/api/quick-setup'

export type { ClusterData, ClusterStatus, ClusterType }

/** 列表行:集群 + 节点/GPU 聚合统计 */
export type NodePoolRecord = ClusterData & {
  /** 集群内节点数 */
  nodeCount: number
  /** 集群内 GPU 总量 */
  gpuTotal: number
}

/** 创建/编辑表单值 */
export interface NodePoolFormValues {
  id?: number
  name: string
  type: ClusterType
  description: string
}

/** 集群类型选项 */
export const CLUSTER_TYPE_OPTIONS: { label: string; value: ClusterType }[] = [
  { label: 'Docker', value: 'docker' },
  { label: 'Kubernetes', value: 'k8s' },
]

/** 状态文案与标签类型映射(供表格渲染) */
export const clusterStatusMap: Record<
  ClusterStatus,
  { label: string; type: 'success' | 'default' }
> = {
  active: { label: '启用', type: 'success' },
  inactive: { label: '停用', type: 'default' },
}

/** 变更监听器:写操作完成后触发,供表格 refresh 联动 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/**
 * 集群(集群)数据 store:表格请求、表单保存、状态切换、删除均走此单例,
 * 数据来源于 /api/v1/admin/clusters 接口;节点/GPU 统计由 nodes/gpus 接口聚合。
 */
export function useStore() {
  /** 分页请求(供 useTable.request);节点数/GPU 总量按全量节点(上限 100)聚合 */
  const request = async (params: { page: number; pageSize: number }, ctx?: TableRequestContext) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    const [clusterPage, nodePage, gpuPage] = await Promise.all([
      listClusters({ page: params.page, page_size: pageSize }, ctx?.signal),
      listNodes({ page: 1, page_size: 100 }, ctx?.signal),
      listGpus({ page: 1, page_size: 100 }, ctx?.signal),
    ])
    // 节点 → 集群归属映射,供 GPU 总量按集群聚合
    const nodeCluster = new Map(nodePage.items.map((node) => [node.id, node.cluster_id]))
    const nodeCountMap = new Map<number, number>()
    for (const node of nodePage.items) {
      nodeCountMap.set(node.cluster_id, (nodeCountMap.get(node.cluster_id) ?? 0) + 1)
    }
    const gpuCountMap = new Map<number, number>()
    for (const gpu of gpuPage.items) {
      const clusterId = nodeCluster.get(gpu.node_id)
      if (clusterId != null) {
        gpuCountMap.set(clusterId, (gpuCountMap.get(clusterId) ?? 0) + 1)
      }
    }
    const items: NodePoolRecord[] = clusterPage.items.map((cluster) => ({
      ...cluster,
      nodeCount: nodeCountMap.get(cluster.id) ?? 0,
      gpuTotal: gpuCountMap.get(cluster.id) ?? 0,
    }))
    return { data: items, total: clusterPage.total }
  }

  /** 新增或更新:依据 model.id 判断;返回操作结果供调用方提示(类型创建后不可改) */
  const save = async (
    model: NodePoolFormValues,
  ): Promise<{ mode: 'create' | 'update'; name: string }> => {
    let mode: 'create' | 'update'
    if (model.id) {
      await updateCluster(model.id, { name: model.name, description: model.description })
      mode = 'update'
    } else {
      await createCluster({
        name: model.name,
        type: model.type,
        description: model.description,
      })
      // 同步向导守卫的集群存在性缓存,避免绕行创建后仍被拽回向导
      markClusterCreated()
      mode = 'create'
    }
    notify()
    return { mode, name: model.name }
  }

  /** 启用/停用:active ↔ inactive 切换,返回切换后状态 */
  const toggleStatus = async (row: NodePoolRecord): Promise<ClusterStatus> => {
    const next: ClusterStatus = row.status === 'active' ? 'inactive' : 'active'
    await updateCluster(row.id, { status: next })
    notify()
    return next
  }

  /** 删除集群(集群下仍有节点时会被后端拒绝) */
  const removePool = async (id: number): Promise<void> => {
    await deleteCluster(id)
    // 删除后缓存可能失真(空集群),强制守卫下次重新探测
    resetClusterCache()
    notify()
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { request, save, toggleStatus, removePool, subscribe }
}
