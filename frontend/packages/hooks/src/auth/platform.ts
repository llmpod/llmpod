import { getLastPlatform, getToken } from './tokens'
import type { Platform } from './types'

/**
 * 返回当前应进入的平台。
 * 双端可同时登录(合法状态):优先「最近登录平台」——标记存在且该平台 token 仍在时直接采用;
 * 标记缺失或失效(残留)时回退按 token 探测,tenant 优先,与历史行为一致。
 * 都不存在返回 null。
 */
export function getActivePlatform(): Platform | null {
  const last = getLastPlatform()
  if (last && getToken(last)) return last
  if (getToken('tenant')) return 'tenant'
  if (getToken('admin')) return 'admin'
  return null
}
