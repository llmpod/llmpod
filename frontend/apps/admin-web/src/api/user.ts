import { request } from './request'

/** 用户状态：active=启用 | disabled=禁用 */
export type UserStatus = 'active' | 'disabled'

/** 用户信息（对应后端 UserSchema） */
export interface UserData {
  id: number
  /** 所属租户ID */
  tenant_id: number
  username: string
  email: string | null
  phone: string | null
  status: UserStatus
  /** 最近一次登录时间（可空） */
  last_login_at: string | null
  created_at: string
  updated_at: string
  /** 绑定的角色ID列表（后端已降级，无关联时为 []，不会缺字段） */
  role_ids: number[]
  /** 所属租户总余额（元，Decimal 字符串，聚合装配；平台用户恒为平台租户余额 0） */
  tenant_balance: string
  /** 所属租户在途冻结（元，Decimal 字符串，聚合装配） */
  tenant_frozen_balance: string
  /** 所属租户可用余额（元，Decimal 字符串，聚合装配；= balance - frozen_balance） */
  tenant_available: string
}

/** 用户分页数据（GET /api/v1/admin/users，固定只含 tenant_id=0 平台用户） */
export interface UserPageData {
  items: UserData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 列出平台用户查询参数（固定只返 tenant_id=0 平台用户，无 tenant_id 参数） */
export interface ListUsersParams {
  /** 页码，从1开始 */
  page?: number
  /** 每页条数，最大100 */
  page_size?: number
}

/** 列出平台用户（GET /api/v1/admin/users，仅限平台管理员；作用域固化为 tenant_id=0） */
export function listUsers(params: ListUsersParams, signal?: AbortSignal): Promise<UserPageData> {
  return request({ url: '/admin/users', method: 'GET', params, signal })
}

/** 创建平台用户请求体（username / password / role_ids 必填；tenant_id 由后端固定为 0） */
export interface CreateUserPayload {
  username: string
  password: string
  /** 初始角色ID列表（必须为平台域角色 tenant_id=0，否则 Conflict） */
  role_ids: number[]
  email?: string
  phone?: string
}

/** 创建用户（POST /api/v1/admin/users） */
export function createUser(data: CreateUserPayload): Promise<UserData> {
  return request({ url: '/admin/users', method: 'POST', data })
}

/** 获取平台用户详情（GET /api/v1/admin/users/{user_id}，非 tenant_id=0 一律 404） */
export function getUser(userId: number): Promise<UserData> {
  return request({ url: `/admin/users/${userId}`, method: 'GET' })
}

/** 更新用户请求体：均为可选，不传则不修改；username 与 tenant_id 不可改 */
export interface UpdateUserPayload {
  /** 新密码（明文传入，服务端哈希后覆盖） */
  password?: string
  email?: string
  phone?: string
  status?: UserStatus
}

/** 更新平台用户（POST /api/v1/admin/users/{user_id}/update，非 tenant_id=0 一律 404；角色管理走 /admin/access） */
export function updateUser(userId: number, data: UpdateUserPayload): Promise<UserData> {
  return request({ url: `/admin/users/${userId}/update`, method: 'POST', data })
}

/** 删除平台用户（POST /api/v1/admin/users/{user_id}/delete，非 tenant_id=0 一律 404；级联清理角色关联） */
export function deleteUser(userId: number): Promise<unknown> {
  return request({ url: `/admin/users/${userId}/delete`, method: 'POST' })
}
