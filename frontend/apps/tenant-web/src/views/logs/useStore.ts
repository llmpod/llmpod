import type { TableRequestContext } from '@repo/hooks'
import {
  listTenantAuditLogs,
  type AuditAction,
  type AuditLogData,
  type AuditResult,
} from '@/api/audit-log'

export type { AuditAction, AuditLogData, AuditResult }

/** 操作文案与标签类型映射(后端受控词表;未知枚举回退原串) */
export const actionMap: Record<
  string,
  { label: string; type: 'primary' | 'info' | 'success' | 'warning' | 'error' | 'default' }
> = {
  create: { label: '创建', type: 'primary' },
  update: { label: '更新', type: 'info' },
  delete: { label: '删除', type: 'error' },
  login: { label: '登录', type: 'success' },
  logout: { label: '登出', type: 'default' },
}

/** 结果文案与标签类型映射 */
export const resultMap: Record<string, { label: string; type: 'success' | 'error' }> = {
  success: { label: '成功', type: 'success' },
  failed: { label: '失败', type: 'error' },
}

/**
 * 资源类型文案映射(对齐后端权限 resource 串,开放词表;
 * 未收录的类型展示时回退原始串)。
 */
export const resourceTypeMap: Record<string, string> = {
  user: '用户',
  tenant: '租户',
  role: '角色',
  api_key: 'API Key',
  cluster: '集群',
  node: '节点',
  model: '模型',
  deployment: '部署',
  endpoint: '端点',
  route: '路由',
  binding: '路由绑定',
  wallet: '钱包',
  system_config: '系统配置',
}

/** 资源类型展示文案:未收录回退原始串,空值占位 */
export function resourceTypeLabel(value: string | null): string {
  if (!value) return '—'
  return resourceTypeMap[value] ?? value
}

/** 操作筛选选项(含「全部」占位) */
export const actionFilterOptions = [
  { label: '全部操作', value: '' },
  ...Object.entries(actionMap).map(([value, conf]) => ({ label: conf.label, value })),
]

/** 日期筛选禁用未来日期(日志只可能发生在过去;今天可选,明天起禁用) */
export const disableFutureDate = (ts: number) => ts > new Date().setHours(23, 59, 59, 999)

/**
 * daterange 日期区间归一化为整天范围:[起始日 00:00:00.000, 结束日 23:59:59.999]。
 * naive-ui 的 default-time 仅对 datetimerange 生效,daterange 选中同一天时
 * 起止均为当日 0 点,直接透传会得到空区间查不出数据。
 */
export function normalizeDateRange(range: [number, number] | null): [number, number] | null {
  if (!range) return null
  const start = new Date(range[0])
  const end = new Date(range[1])
  return [
    new Date(start.getFullYear(), start.getMonth(), start.getDate()).getTime(),
    new Date(end.getFullYear(), end.getMonth(), end.getDate(), 23, 59, 59, 999).getTime(),
  ]
}

/** ISO8601 → YYYY-MM-DD HH:mm:ss(本地时区);空值 '—',非法原样返回 */
export function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

/** 查询参数(与表格 param 对齐;作用域硬绑当前租户,无 tenant_id;dateRange 为毫秒时间戳区间) */
export interface AuditQuery {
  action: AuditAction | ''
  /** 按资源 ID 快照精确过滤 */
  resource_id: string
  dateRange: [number, number] | null
}

/**
 * 审计日志数据 store:日志为只读流水,无写操作,
 * 故无 notify/subscribe,仅提供分页 + 筛选请求。
 */
export function useStore() {
  /** 分页 + 筛选请求(供 useTable.request) */
  const request = async (
    params: AuditQuery & { page: number; pageSize: number },
    context?: TableRequestContext,
  ) => {
    const data = await listTenantAuditLogs(
      {
        page: params.page,
        page_size: params.pageSize,
        action: params.action || undefined,
        resource_id: params.resource_id.trim() || undefined,
        since_ms: params.dateRange?.[0],
        until_ms: params.dateRange?.[1],
      },
      context?.signal,
    )
    return { data: data.items, total: data.total }
  }

  return { request }
}
