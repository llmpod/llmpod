import { request } from './request'

/** 当前用户信息（GET /api/v1/auth/me） */
export interface UserInfoData {
  user_id: number
  username: string
  tenant_id: number
  role_ids: number[]
  /** 访问域：platform=管理员端，tenant=租户端 */
  access_domain: string
  is_superuser: boolean
}

/** 获取当前用户信息 */
export function getUserInfo(): Promise<UserInfoData> {
  return request({ url: '/auth/me', method: 'GET' })
}

/** 菜单可见性标注(GET /auth/menus 返回的 value):enabled=false 表示有权限但被本人自助开关隐藏 */
export interface MenuItemData {
  enabled: boolean
}

/**
 * 获取当前用户可见菜单（GET /api/v1/auth/menus）。
 * 返回字典:key=菜单路径(如 /tenant/console),key 不存在=无功能权限;
 * enabled=false=有权但被本人自助关闭(呈现层隐藏,直接访问仍被守卫拦截)。超管返回本域全集。
 */
export function getMenus(): Promise<Record<string, MenuItemData>> {
  return request({ url: '/auth/menus', method: 'GET' })
}

/** 自助保存后的回显(POST /auth/menus/update 的 data):本人最终下线集 */
export interface UserMenusState {
  /** 用户 ID(即当前会话用户) */
  user_id: number
  /** 本人关闭的本域菜单路径(全量覆盖后) */
  disabled: string[]
}

/**
 * 自助修改本人菜单可见性（POST /api/v1/auth/menus/update）。
 * disabled 即最终态(全量覆盖):空数组=全部显示;不收 user_id,只作用于当前登录用户,
 * 后端按 access_domain 选域,跨域/未声明路径返回 422(错误提示由 http 层统一弹出)。
 */
export function updateMyMenus(disabled: string[]): Promise<UserMenusState> {
  return request({ url: '/auth/menus/update', method: 'POST', data: { disabled } })
}

/** 用户登出：使令牌立即失效（POST /api/v1/auth/logout） */
export function logout(): Promise<unknown> {
  return request({ url: '/auth/logout', method: 'POST' })
}

/** 自助改密请求体（只作用于当前会话用户，不收 user_id） */
export interface ChangePasswordPayload {
  /** 当前密码（校验通过才允许修改；错误返回 401 INVALID_CREDENTIALS） */
  old_password: string
  /** 新密码（明文传入，服务端哈希存储） */
  new_password: string
}

/**
 * 修改当前用户密码（POST /api/v1/auth/change-password）。
 * 成功后当前 token 立即失效：调用方需先 toast 再清 token 跳登录页。
 * skipUnauthorized：旧密码错误返回 401 时走普通错误提示（onError），
 * 不触发全局「登录已失效」登出流程（会话本身仍有效）。
 */
export function changePassword(data: ChangePasswordPayload): Promise<{ changed: boolean }> {
  return request({
    url: '/auth/change-password',
    method: 'POST',
    data,
    meta: { skipUnauthorized: true },
  })
}
