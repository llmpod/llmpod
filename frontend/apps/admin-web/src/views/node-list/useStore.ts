import type { TableRequestContext } from '@repo/hooks'
import {
  listNodes,
  getNode,
  updateNode,
  deleteNode,
  type NodeData,
  type NodeRuntime,
  type NodeStatus,
  type NodeVersionStatus,
} from '@/api/node'
import { listGpus, type GpuData, type GpuRuntime, type GpuStatus } from '@/api/gpu'

export type { NodeData, NodeRuntime, NodeStatus, NodeVersionStatus, GpuData, GpuRuntime, GpuStatus }

/** 列表行:节点 + 该节点的 GPU 列表(由 GPU 接口按 node_id 聚合) */
export type ComputeNode = NodeData & { gpus: GpuData[] }

/** 状态文案与标签类型映射(供表格渲染) */
export const nodeStatusMap: Record<
  NodeStatus,
  { label: string; type: 'success' | 'warning' | 'error' | 'info' | 'default' }
> = {
  online: { label: '在线', type: 'success' },
  offline: { label: '离线', type: 'error' },
  maintenance: { label: '维护', type: 'warning' },
  busy: { label: '繁忙', type: 'info' },
  drained: { label: '已排空', type: 'default' },
}

/** GPU 状态文案与标签类型映射(供 GPU 抽屉/弹窗渲染) */
export const gpuStatusMap: Record<
  GpuStatus,
  { label: string; type: 'success' | 'warning' | 'error' }
> = {
  online: { label: '在线', type: 'success' },
  stale: { label: '心跳过期', type: 'warning' },
  offline: { label: '离线', type: 'error' },
}

/** 节点版本状态文案与标签类型映射(比对基准是 manager 版本,后端已比较,前端只渲染四态) */
export const nodeVersionStatusMap: Record<
  NodeVersionStatus,
  { label: string; type: 'success' | 'warning' | 'info' | 'default' }
> = {
  ok: { label: '同版本', type: 'success' },
  outdated: { label: '可升级', type: 'warning' },
  newer: { label: '超前', type: 'info' },
  unknown: { label: '未上报', type: 'default' },
}

/** ISO8601 时间格式化为 YYYY-MM-DD HH:mm;空值显示 — */
export function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 百分比文案:四舍五入取整 + %,如 12% */
export function formatPercent(value: number): string {
  return `${Math.round(value)}%`
}

/** GB 用量对文案:used/totalGB,如 45.6/64GB;total 缺失显示 — */
export function formatGbPair(used: number, total: number | null): string {
  return `${used}/${total ?? '—'}GB`
}

/** 进度条颜色:<=80 绿色(success),>80 黄色(warning),>90 红色(error) */
export function progressColor(percent: number): string {
  if (percent > 90) return 'bg-error'
  if (percent > 80) return 'bg-warning'
  return 'bg-success'
}

/** 节点 GPU 汇总(表格 GPU/显存/驱动列);无 GPU 时各字段为 — */
export interface GpuSummary {
  /** 型号 × 数量,如 A100 × 4 */
  label: string
  /** 单卡显存,如 80GB */
  vram: string
  /** 驱动版本,未安装显示"未安装" */
  driver: string
}

/** 由节点 GPU 列表推导汇总信息(同型号取首卡) */
export function gpuSummary(gpus: GpuData[]): GpuSummary {
  const first = gpus[0]
  if (!first) return { label: '—', vram: '—', driver: '—' }
  return {
    label: `${first.name} × ${gpus.length}`,
    vram: `${first.total_vram_gb}GB`,
    driver: first.driver_version ?? '未安装',
  }
}

/** 变更监听器:写操作完成后触发,供表格 refresh 联动 */
const listeners = new Set<() => void>()
export function notify(): void {
  listeners.forEach((fn) => fn())
}

/**
 * 节点数据 store:列表请求、详情、维护模式切换、注销均走此单例,
 * 数据来源于 /api/v1/admin/nodes 与 /api/v1/admin/gpus 接口。
 */
export function useStore() {
  /** 分页 + 关键字请求(供 useTable.request);keyword 后端不支持,仅过滤当前页 */
  const request = async (
    params: {
      page: number
      pageSize: number
      keyword?: string
    },
    ctx?: TableRequestContext,
  ) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    // GPU 按节点聚合:一次拉全量 GPU(上限 100),按 node_id 分组挂到节点行上
    const [nodePage, gpuPage] = await Promise.all([
      listNodes({ page: params.page, page_size: pageSize }, ctx?.signal),
      listGpus({ page: 1, page_size: 100 }, ctx?.signal),
    ])
    const gpuMap = new Map<string, GpuData[]>()
    for (const gpu of gpuPage.items) {
      const list = gpuMap.get(gpu.node_id) ?? []
      list.push(gpu)
      gpuMap.set(gpu.node_id, list)
    }
    let items: ComputeNode[] = nodePage.items.map((node) => ({
      ...node,
      gpus: (gpuMap.get(node.id) ?? []).sort((a, b) => a.index - b.index),
    }))
    const keyword = (params.keyword ?? '').trim().toLowerCase()
    if (keyword) {
      items = items.filter(
        (r) => r.hostname.toLowerCase().includes(keyword) || r.ip.includes(keyword),
      )
    }
    return { data: items, total: nodePage.total }
  }

  /** 节点详情(GET /api/v1/admin/nodes/{node_id}) */
  const getDetail = (id: string): Promise<NodeData> => {
    return getNode(id)
  }

  /** 节点 GPU 列表(GET /api/v1/admin/gpus?node_id=,按卡序号排序) */
  const listNodeGpus = async (nodeId: string): Promise<GpuData[]> => {
    const result = await listGpus({ node_id: nodeId, page: 1, page_size: 100 })
    return result.items.sort((a, b) => a.index - b.index)
  }

  /** 维护模式切换:online ↔ maintenance,返回切换后状态 */
  const toggleMaintenance = async (node: NodeData): Promise<NodeStatus> => {
    const next: NodeStatus = node.status === 'maintenance' ? 'online' : 'maintenance'
    await updateNode(node.id, { status: next })
    notify()
    return next
  }

  /** 注销节点(在线节点会被后端拒绝,需先维护/离线) */
  const remove = async (id: string): Promise<void> => {
    await deleteNode(id)
    notify()
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { request, getDetail, listNodeGpus, toggleMaintenance, remove, subscribe }
}
