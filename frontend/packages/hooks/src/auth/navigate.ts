import type { Platform } from './types'

/** 构造目标平台 URL（相对路径，开发期经 portal proxy、生产同域均同源）。 */
export function buildPlatformUrl(platform: Platform, path = '/dashboard'): string {
  const base = platform === 'tenant' ? '/tenant' : '/admin'
  return `${base}${path}`
}

/** 构造 portal 登录页 URL（登录后固定跳转账号平台首页，不使用 redirect 回跳）。 */
export function buildPortalLoginUrl(origin: string = window.location.origin): string {
  return new URL('/login', origin).href
}

/** 整页跳转到目标平台默认首页（或指定路径）。 */
export function goToPlatform(platform: Platform, path?: string): void {
  window.location.href = buildPlatformUrl(platform, path)
}

/** 整页跳转到 portal 登录页。 */
export function goToPortalLogin(): void {
  window.location.href = buildPortalLoginUrl()
}
