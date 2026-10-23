import type { TableRequestContext } from '@repo/hooks'
import { listGpus, type GpuData, type GpuRuntime, type GpuStatus } from '@/api/gpu'
import { listNodes } from '@/api/node'

export type { GpuData, GpuRuntime, GpuStatus }

/** 列表行:GPU + 所属节点主机名(由节点接口聚合) */
export type Gpu = GpuData & { nodeHost: string }

/** 状态文案与标签类型映射(供表格渲染) */
export const gpuStatusMap: Record<
  GpuStatus,
  { label: string; type: 'success' | 'warning' | 'error' }
> = {
  online: { label: '在线', type: 'success' },
  stale: { label: '心跳过期', type: 'warning' },
  offline: { label: '离线', type: 'error' },
}

/** 百分比文案:四舍五入取整 + %,如 12% */
export function formatPercent(value: number): string {
  return `${Math.round(value)}%`
}

/** GB 用量对文案:used/totalGB,如 5.12/22GB */
export function formatGbPair(used: number, total: number): string {
  return `${used}/${total}GB`
}

/** 进度条颜色:<=80 绿色(success),>80 黄色(warning),>90 红色(error) */
export function progressColor(percent: number): string {
  if (percent > 90) return 'bg-error'
  if (percent > 80) return 'bg-warning'
  return 'bg-success'
}

/** GPU 数据 store:表格请求走此单例,数据来源于 /api/v1/admin/gpus 接口 */
export function useStore() {
  /** 分页请求(供 useTable.request);节点主机名一次拉全量节点(上限 100)聚合 */
  const request = async (params: { page: number; pageSize: number }, ctx?: TableRequestContext) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    const [gpuPage, nodePage] = await Promise.all([
      listGpus({ page: params.page, page_size: pageSize }, ctx?.signal),
      listNodes({ page: 1, page_size: 100 }, ctx?.signal),
    ])
    const hostMap = new Map(nodePage.items.map((node) => [node.id, node.hostname]))
    const items: Gpu[] = gpuPage.items.map((gpu) => ({
      ...gpu,
      nodeHost: hostMap.get(gpu.node_id) ?? gpu.node_id,
    }))
    return { data: items, total: gpuPage.total }
  }

  return { request }
}
