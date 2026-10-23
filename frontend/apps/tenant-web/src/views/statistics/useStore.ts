import type { TableRequestContext } from '@repo/hooks'
import {
  listTenantRequestLogs,
  getTenantRequestLog,
  type RequestLogData,
  type RequestLogStatus,
  type RequestLogUpstreamType,
  type RequestLogDetailData,
} from '@/api/request-log'

export type { RequestLogData, RequestLogStatus, RequestLogUpstreamType, RequestLogDetailData }

/** 日志状态文案与标签类型映射 */
export const logStatusMap: Record<
  RequestLogStatus,
  { label: string; type: 'success' | 'error' | 'warning' }
> = {
  success: { label: '成功', type: 'success' },
  error: { label: '失败', type: 'error' },
  interrupted: { label: '中断', type: 'warning' },
}

/** 上游流向文案与标签类型映射 */
export const upstreamTypeMap: Record<
  RequestLogUpstreamType,
  { label: string; type: 'success' | 'info' }
> = {
  local: { label: '本地', type: 'success' },
  external: { label: '外部', type: 'info' },
}

/** 上游流向展示配置：null=未尝试转发（如无候选端点），显示灰色「未转发」 */
export function upstreamTypeConf(
  value: RequestLogUpstreamType | null,
): { label: string; type: 'success' | 'info' | 'default' } {
  return value == null ? { label: '未转发', type: 'default' } : upstreamTypeMap[value]
}

/** 状态筛选选项(含「全部」占位) */
export const statusFilterOptions = [
  { label: '全部状态', value: '' },
  { label: '成功', value: 'success' },
  { label: '失败', value: 'error' },
  { label: '中断', value: 'interrupted' },
]

/** 日期筛选禁用未来日期(调用日志只可能发生在过去;今天可选,明天起禁用) */
export const disableFutureDate = (ts: number) => ts > new Date().setHours(23, 59, 59, 999)

/** Decimal 字符串去尾零展示("12.500000" → "12.5","0.0000005" 不进科学计数法);空值显示 — */
export { formatDecimal } from '@repo/hooks'

/** 千分位格式化数字;空值显示 — */
export function formatNumber(value: number | null): string {
  if (value == null) return '—'
  return value.toLocaleString('zh-CN')
}

/** ISO8601 时间格式化为 YYYY-MM-DD HH:mm;空值显示 — */
export function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * 调用日志数据 store:表格请求、详情均走此单例,
 * 数据来源于 /api/v1/tenant/request-logs 接口(只读页,无写操作)。
 */
export function useStore() {
  /** 分页 + 筛选请求(供 useTable.request);status/model/api_key_id/dateRange 透传后端筛选 */
  const request = async (
    params: {
      page: number
      pageSize: number
      status?: string
      model?: string
      api_key_id?: number | null
      dateRange?: [number, number] | null
    },
    ctx?: TableRequestContext,
  ) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    const result = await listTenantRequestLogs(
      {
        page: params.page,
        page_size: pageSize,
        status: (params.status || undefined) as RequestLogStatus | undefined,
        model: params.model?.trim() || undefined,
        api_key_id: params.api_key_id ?? undefined,
        since_ms: params.dateRange?.[0] ?? undefined,
        until_ms: params.dateRange?.[1] ?? undefined,
      },
      ctx?.signal,
    )
    return { data: result.items, total: result.total }
  }

  /** 调用日志详情(含 request_body/response_body) */
  const getDetail = async (requestId: string): Promise<RequestLogDetailData> => {
    return getTenantRequestLog(requestId)
  }

  return { request, getDetail }
}
