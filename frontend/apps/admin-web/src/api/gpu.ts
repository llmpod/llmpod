import { request } from './request'

/** GPU 状态：online=在线 | stale=心跳过期 | offline=离线 */
export type GpuStatus = 'online' | 'stale' | 'offline'

/** GPU 实时指标摘要（来自节点心跳镜像；节点离线/未上报时为 null） */
export interface GpuRuntime {
  /** 已用显存（GB） */
  used_vram_gb: number
  /** 显存利用率 0-100 */
  memory_utilization: number
  /** GPU 核心利用率 0-100 */
  utilization: number
  /** 温度（℃） */
  temperature: number
  /** 风扇转速百分比 0-100 */
  fan_speed: number
  /** 当前功耗（W） */
  power_draw_w: number
  /** 功耗上限（W） */
  power_limit_w: number
  /** SM 时钟频率（MHz） */
  sm_clock_mhz: number
  /** 显存时钟频率（MHz） */
  mem_clock_mhz: number
  /** GPU 上下文进程数 */
  process_count: number
}

/** GPU 信息（对应后端 GpuSchema） */
export interface GpuData {
  /** 主键ID（自增） */
  id: number
  /** GPU 硬件 UUID（全局唯一） */
  uuid: string
  /** 所属节点ID */
  node_id: string
  /** GPU 在节点内的序号（从 0 开始） */
  index: number
  /** GPU 型号名称 */
  name: string
  /** GPU 架构，如 ampere / hopper / ascend910 */
  arch: string
  /** 显存总量（GB） */
  total_vram_gb: number
  /** 驱动版本（可空） */
  driver_version: string | null
  status: GpuStatus
  created_at: string
  updated_at: string
  /** 所属节点主机名（派生字段，无关联时为 null） */
  node_hostname: string | null
  /** 所属集群ID（派生字段，无关联时为 null） */
  cluster_id: number | null
  /** 所属集群名（派生字段，无关联时为 null） */
  cluster_name: string | null
  /** 实时指标摘要（节点离线/心跳未上报时为 null） */
  runtime: GpuRuntime | null
}

/** GPU 分页数据（GET /api/v1/admin/gpus） */
export interface GpuPageData {
  items: GpuData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 列出GPU查询参数 */
export interface ListGpusParams {
  /** 节点ID */
  node_id?: string
  /** GPU状态，不传则列出全部 */
  status?: GpuStatus
  /** 页码，从1开始 */
  page?: number
  /** 每页条数，最大100 */
  page_size?: number
}

/** 列出GPU（GET /api/v1/admin/gpus） */
export function listGpus(params: ListGpusParams, signal?: AbortSignal): Promise<GpuPageData> {
  return request({ url: '/admin/gpus', method: 'GET', params, signal })
}

/** 获取GPU详情（GET /api/v1/admin/gpus/{gpu_id}） */
export function getGpu(gpuId: number): Promise<GpuData> {
  return request({ url: `/admin/gpus/${gpuId}`, method: 'GET' })
}
