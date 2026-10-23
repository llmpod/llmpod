import { request } from './request'

/** 操作类型：创建 | 更新 | 删除 | 登录 | 登出 */
export type AuditAction = 'create' | 'update' | 'delete' | 'login' | 'logout'

/** 操作结果：success=成功 | failed=失败 */
export type AuditResult = 'success' | 'failed'

/** 审计日志列表项（对应后端 AuditLogSchema；detail 已脱敏，列表行即详情数据源） */
export interface AuditLogData {
  /** 主键 ID */
  id: number
  /** 操作者类型：user=用户 | system=系统 */
  actor_type: 'user' | 'system'
  /** 操作者 ID（逻辑外键 → user.id；0=未知主体，如登录失败尝试） */
  actor_id: number
  /** 操作者用户名快照（可空；登录失败尝试为所试用户名） */
  actor_name: string | null
  /** 数据归属租户 ID（全局操作为空） */
  tenant_id: number | null
  /** 操作：create | update | delete | login | logout */
  action: AuditAction
  /** 操作结果：success | failed */
  result: AuditResult
  /** 资源类型（对齐权限 resource 串：user/tenant/api_key/...；登录登出为空） */
  resource_type: string | null
  /** 资源 ID 快照（可空） */
  resource_id: string | null
  /** 明细 JSONB：params（脱敏后请求参数）/ 返回摘要 / error */
  detail: Record<string, unknown> | null
  /** 操作来源 IP（可空） */
  ip_address: string | null
  /** 操作时间（ISO8601） */
  created_at: string
}

/** 审计日志分页数据（对应后端 PageData[AuditLogSchema]） */
export interface AuditLogPageData {
  items: AuditLogData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 审计日志查询参数（admin 端含 tenant_id；均可空） */
export interface ListAuditLogsParams {
  /** 页码，从 1 开始，默认 1 */
  page?: number
  /** 每页条数，1~100，默认 20 */
  page_size?: number
  /** 按数据归属租户 ID 过滤（不传=全量） */
  tenant_id?: number | null
  /** 按操作者 ID 过滤 */
  actor_id?: number | null
  /** 按操作过滤：create | update | delete | login | logout */
  action?: AuditAction | null
  /** 按资源类型过滤：user | tenant | api_key | ... */
  resource_type?: string | null
  /** 按资源 ID 快照过滤 */
  resource_id?: string | null
  /** 按结果过滤：success | failed */
  result?: AuditResult | null
  /** 起始时间（毫秒时间戳，含） */
  since_ms?: number | null
  /** 截止时间（毫秒时间戳，含） */
  until_ms?: number | null
}

/** 平台全量审计日志分页查询（GET /api/v1/admin/audit-logs；需 AUDIT_LOG_READ 权限） */
export function listAuditLogs(
  params: ListAuditLogsParams,
  signal?: AbortSignal,
): Promise<AuditLogPageData> {
  return request({ url: '/admin/audit-logs', method: 'GET', params, signal })
}
