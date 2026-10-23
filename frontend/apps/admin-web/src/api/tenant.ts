import { request } from './request'

/** 租户状态：active=启用 | disabled=禁用 */
export type TenantStatus = 'active' | 'disabled'

/** 被封禁的模型（对应后端 BlockedModelOut：封禁行 join 路由表解析出的名称/类别） */
export interface BlockedModelData {
  route_id: number
  name: string
  /** 模型类别：llm | embedding | rerank | ... */
  model_type: string
}

/** 租户信息（对应后端 TenantDetailOut） */
export interface TenantData {
  id: number
  name: string
  status: TenantStatus
  created_at: string
  updated_at: string
  /** 用户总数（派生统计字段，后端已降级，不会缺字段） */
  user_count: number
  /** 角色总数（派生统计字段，后端已降级，不会缺字段） */
  role_count: number
  /** 总余额（元，Decimal 字符串，聚合装配；预扣不动，充值/调额/真实扣费才变） */
  balance: string
  /** 在途冻结（元，Decimal 字符串，聚合装配；预扣时 +，结算/解冻时 -） */
  frozen_balance: string
  /** 可用余额（元，Decimal 字符串，聚合装配；= balance - frozen_balance） */
  available: string
  /** 被封禁的模型列表（聚合装配，默认 []；默认放行其余全部已发布路由，此列表为封禁例外） */
  blocked_models: BlockedModelData[]
}

/** 租户分页数据（GET /api/v1/admin/tenants） */
export interface TenantPageData {
  items: TenantData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 列出租户查询参数 */
export interface ListTenantsParams {
  /** 租户状态，不传则列出全部 */
  status?: TenantStatus
  /** 页码，从1开始 */
  page?: number
  /** 每页条数，最大100 */
  page_size?: number
}

/** 列出租户（GET /api/v1/admin/tenants，仅限平台管理员） */
export function listTenants(
  params: ListTenantsParams,
  signal?: AbortSignal,
): Promise<TenantPageData> {
  return request({ url: '/admin/tenants', method: 'GET', params, signal })
}

/** 创建租户请求体（name / admin_username / admin_password 必填；后端写死绑 tenant_admin 角色） */
export interface CreateTenantPayload {
  name: string
  /** 租户超管用户名（创建后即可直接登录租户端） */
  admin_username: string
  /** 租户超管密码（明文传入，服务端哈希存储） */
  admin_password: string
  admin_email?: string
  admin_phone?: string
  /** 封禁的路由ID列表（可选；默认放行全部已发布路由，此列表为封禁例外） */
  blocked_route_ids?: number[]
}

/** 创建租户（POST /api/v1/admin/tenants，同步创建内置租户角色与租户超管） */
export function createTenant(data: CreateTenantPayload): Promise<TenantData> {
  return request({ url: '/admin/tenants', method: 'POST', data })
}

/** 获取租户详情（GET /api/v1/admin/tenants/{tenant_id}） */
export function getTenant(tenantId: number): Promise<TenantData> {
  return request({ url: `/admin/tenants/${tenantId}`, method: 'GET' })
}

/** 更新租户请求体：均为可选，不传则不修改 */
export interface UpdateTenantPayload {
  name?: string
  status?: TenantStatus
  /** 封禁的路由ID列表（全量替换语义：不传=不改；传 []=清空封禁；传 [1,2]=全量替换） */
  blocked_route_ids?: number[]
}

/** 更新租户（POST /api/v1/admin/tenants/{tenant_id}/update） */
export function updateTenant(
  tenantId: number,
  data: UpdateTenantPayload,
): Promise<TenantData> {
  return request({ url: `/admin/tenants/${tenantId}/update`, method: 'POST', data })
}

/** 删除租户（POST /api/v1/admin/tenants/{tenant_id}/delete，级联清空该租户用户及其角色后直接删除） */
export function deleteTenant(tenantId: number): Promise<unknown> {
  return request({ url: `/admin/tenants/${tenantId}/delete`, method: 'POST' })
}

/** 租户概览·消费趋势系列（对应后端 TenantOverviewTrendOut） */
export interface TenantOverviewTrendData {
  tenant_id: number
  tenant_name: string
  /** 逐日消费额（元，与共享 X 轴 dates 对位；空日为 0） */
  data: number[]
}

/** 租户概览·余额 Top 行（对应后端 TenantOverviewBalanceOut；金额为 Decimal 字符串） */
export interface TenantOverviewBalanceData {
  tenant_id: number
  tenant_name: string
  /** 总余额（元） */
  balance: string
  /** 可用余额（元 = balance - frozen_balance） */
  available: string
}

/** 租户概览整屏聚合（GET /api/v1/admin/tenants/overview） */
export interface TenantsOverviewData {
  /** 租户总数（不含平台租户） */
  total_tenants: number
  /** 正常租户数（status=active） */
  active_tenants: number
  /** 禁用租户数（status=disabled，「冻结/预逾期」卡口径） */
  disabled_tenants: number
  /** 租户用户总数（平台用户不计） */
  total_users: number
  /** 消费趋势共享 X 轴（近 days 天 ISO 日期） */
  dates: string[]
  /** 消费趋势 top_n（按窗口消费总额降序） */
  consume_trend: TenantOverviewTrendData[]
  /** 余额 top_n（按可用余额降序） */
  balance_top: TenantOverviewBalanceData[]
}

/** 租户概览查询参数（days 1-90 默认 14；top_n 1-20 默认 5） */
export interface GetTenantsOverviewParams {
  days?: number
  top_n?: number
}

/** 租户概览整屏聚合（GET /api/v1/admin/tenants/overview，统计卡 + 消费趋势 + 余额 Top） */
export function getTenantsOverview(
  params?: GetTenantsOverviewParams,
  signal?: AbortSignal,
): Promise<TenantsOverviewData> {
  return request({ url: '/admin/tenants/overview', method: 'GET', params, signal })
}

/** 租户钱包（对应后端 WalletSchema；金额为 Decimal 字符串，available 需前端按 balance-frozen 计算） */
export interface TenantWalletData {
  id: number
  tenant_id: number
  /** 总余额（元）：只被充值/调额/真实扣费改动，预扣不动 */
  balance: string
  /** 在途冻结（元）：预扣时 +、结算/解冻时 - */
  frozen_balance: string
  created_at: string
  updated_at: string
}

/** 调额模式：add=增加 | subtract=扣减 | override=覆盖为 */
export type WalletAdjustMode = 'add' | 'subtract' | 'override'

/** 人工调额请求体（amount：add/subtract 为正数；override 为目标值） */
export interface AdjustTenantWalletPayload {
  amount: number
  mode: WalletAdjustMode
  /** 调额原因（写入流水 meta，可选） */
  reason?: string | null
}

/** 调整租户余额（POST /api/v1/admin/tenants/{tenant_id}/wallet/adjust，返回调整后钱包） */
export function adjustTenantWallet(
  tenantId: number,
  data: AdjustTenantWalletPayload,
): Promise<TenantWalletData> {
  return request({ url: `/admin/tenants/${tenantId}/wallet/adjust`, method: 'POST', data })
}
