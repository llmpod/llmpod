import { request } from './request'

/** 用户状态：active=启用 | disabled=禁用 */
export type MemberStatus = 'active' | 'disabled'

/** 租户成员信息（对应后端 UserDetailOut） */
export interface TenantUserData {
  id: number
  /** 所属租户ID */
  tenant_id: number
  /** 用户名（全局唯一，登录用） */
  username: string
  email: string | null
  phone: string | null
  status: MemberStatus
  /** 最近一次登录时间（可空） */
  last_login_at: string | null
  created_at: string
  updated_at: string
  /** 已绑定的角色ID列表（后端装配，无关联时为 []） */
  role_ids: number[]
  /** 所属租户总余额（元，Decimal 字符串，关联装配；同租户所有用户行同值） */
  tenant_balance: string
  /** 所属租户在途冻结（元，Decimal 字符串，关联装配） */
  tenant_frozen_balance: string
  /** 所属租户可用余额（元，Decimal 字符串，关联装配；= balance - frozen_balance） */
  tenant_available: string
}

/** 创建租户用户请求体（username / password / role_ids 必填；角色必须属当前租户） */
export interface CreateTenantUserPayload {
  username: string
  password: string
  /** 初始角色ID列表（需为当前租户已存在的角色） */
  role_ids: number[]
  email?: string
  phone?: string
}

/** 更新租户用户请求体：均为可选，不传则不修改；username 不可改 */
export interface UpdateTenantUserPayload {
  /** 新密码（明文传入，服务端哈希后覆盖）；不传则不改密 */
  password?: string
  email?: string
  phone?: string
  status?: MemberStatus
}

/** 列出当前租户成员（GET /api/v1/tenant/users，全量返回，无分页参数） */
export function listTenantUsers(signal?: AbortSignal): Promise<TenantUserData[]> {
  return request({ url: '/tenant/users', method: 'GET', signal })
}

/** 创建租户用户（POST /api/v1/tenant/users） */
export function createTenantUser(data: CreateTenantUserPayload): Promise<TenantUserData> {
  return request({ url: '/tenant/users', method: 'POST', data })
}

/** 获取租户用户详情（GET /api/v1/tenant/users/{user_id}） */
export function getTenantUser(userId: number): Promise<TenantUserData> {
  return request({ url: `/tenant/users/${userId}`, method: 'GET' })
}

/** 更新租户用户（POST /api/v1/tenant/users/{user_id}/update，支持资料 / 状态 / 重置密码） */
export function updateTenantUser(
  userId: number,
  data: UpdateTenantUserPayload,
): Promise<TenantUserData> {
  return request({ url: `/tenant/users/${userId}/update`, method: 'POST', data })
}

/** 删除租户用户（POST /api/v1/tenant/users/{user_id}/delete，级联清理角色关联） */
export function deleteTenantUser(userId: number): Promise<unknown> {
  return request({ url: `/tenant/users/${userId}/delete`, method: 'POST' })
}
