import type { Platform } from './types'

export const TENANT_TOKEN_KEY = 'tenant_access_token'
export const ADMIN_TOKEN_KEY = 'admin_access_token'
/** 最近登录平台标记:双端同时登录时,portal 分发据此决定去向 */
export const LAST_PLATFORM_KEY = 'llmpod:last-platform'

function keyOf(platform: Platform): string {
  return platform === 'tenant' ? TENANT_TOKEN_KEY : ADMIN_TOKEN_KEY
}

/** 读取指定平台的访问令牌；不存在返回空字符串。 */
export function getToken(platform: Platform): string {
  return localStorage.getItem(keyOf(platform)) || ''
}

/** 写入指定平台的访问令牌,同时刷新「最近登录平台」标记。 */
export function setToken(platform: Platform, token: string): void {
  localStorage.setItem(keyOf(platform), token)
  localStorage.setItem(LAST_PLATFORM_KEY, platform)
}

/** 清除指定平台的访问令牌;若其同时为最近登录平台,一并清除标记(剩余 token 由探测兜底)。 */
export function clearToken(platform: Platform): void {
  localStorage.removeItem(keyOf(platform))
  if (getLastPlatform() === platform) localStorage.removeItem(LAST_PLATFORM_KEY)
}

/** 清除所有平台的访问令牌与最近登录标记（异常残留兜底）。 */
export function clearAllTokens(): void {
  localStorage.removeItem(TENANT_TOKEN_KEY)
  localStorage.removeItem(ADMIN_TOKEN_KEY)
  localStorage.removeItem(LAST_PLATFORM_KEY)
}

/** 读取最近登录平台标记;值非法或不存在返回 null。 */
export function getLastPlatform(): Platform | null {
  const value = localStorage.getItem(LAST_PLATFORM_KEY)
  return value === 'tenant' || value === 'admin' ? value : null
}
