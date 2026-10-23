import { request } from './request'

/** 趋势单点（近 7 日逐日；空日补 0） */
export interface TenantTrendPointData {
  /** 日期（ISO8601，本地时区） */
  date: string
  /** 当日调用量（触达上游） */
  call_count: number
  /** 当日错误率 0-1 */
  error_rate: number
}

/** 可用模型简行（published − 封禁） */
export interface TenantModelBriefData {
  /** 路由 ID */
  id: number
  /** 模型名（对外别名） */
  name: string
}

/** 最近调用行（top5） */
export interface TenantRecentCallData {
  /** 模型名（对外别名，可空） */
  model: string | null
  input_tokens: number
  output_tokens: number
  /** 端到端延迟毫秒（可空） */
  latency_ms: number | null
  /** 结果：success | error | interrupted */
  status: string
  /** 时间（ISO8601，可空） */
  created_at: string | null
}

/** 租户控制台整屏聚合（GET /api/v1/tenant/dashboard/screen；金额为 Decimal 字符串，元） */
export interface TenantDashboardScreenData {
  /** 窗口内调用次数 */
  calls: number
  /** 窗口内有流量的模型数 */
  active_models: number
  /** 窗口内错误率 0-1 */
  error_rate: number
  /** 窗口内总 token */
  total_tokens: number
  /** 平均首帧延迟毫秒（仅流式；可空） */
  avg_ttft_ms: number | null
  /** 近 7 日调用趋势（空日补 0） */
  trend: TenantTrendPointData[]
  /** 本租户可用模型 */
  models: TenantModelBriefData[]
  /** 最近调用（前 5 条） */
  recent_calls: TenantRecentCallData[]
  /** 总余额（元） */
  balance: string
  /** 在途冻结（元） */
  frozen_balance: string
  /** 可用余额（元 = balance - frozen_balance） */
  available: string
  /** 本月消费（元） */
  month_consume: string
}

/** 时间窗查询参数（缺省 = 今日 00:00 本地 → 当前；数据强制本租户，无越权面） */
export interface TenantDashboardWindowParams {
  from?: string
  to?: string
}

/** 租户控制台整屏聚合（GET /api/v1/tenant/dashboard/screen，单请求） */
export function getTenantDashboardScreen(
  params?: TenantDashboardWindowParams,
  signal?: AbortSignal,
): Promise<TenantDashboardScreenData> {
  return request({ url: '/tenant/dashboard/screen', method: 'GET', params, signal })
}
