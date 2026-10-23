import type { Platform } from '@repo/hooks'
import { request } from '@/http'

export interface LoginPayload {
  username: string
  password: string
}

/** 控制平面登录响应 data（POST /api/v1/auth/login） */
export interface LoginData {
  token: string
  user_id: number
  username: string
  /** 访问域：platform=管理员端，tenant=租户端 */
  access_domain: string
  is_superuser: boolean
}

export interface LoginResult {
  platform: Platform
  accessToken: string
}

/**
 * 登录接口：
 * POST /api/v1/auth/login { username, password }
 * → data: { token, access_domain, ... }
 *
 * 按 access_domain 裁定目标后台：platform→admin，tenant→tenant。
 */
export async function login(payload: LoginPayload): Promise<LoginResult> {
  if (import.meta.env.VITE_USE_AUTH_MOCK === 'true') {
    return mockLogin(payload)
  }
  // skipUnauthorized: 登录失败(code 401)走 onError 提示,不触发登录失效跳转
  const data = await request<LoginData>({
    url: '/login',
    method: 'POST',
    data: payload,
    meta: { skipUnauthorized: true },
  })
  return {
    platform: data.access_domain === 'platform' ? 'admin' : 'tenant',
    accessToken: data.token,
  }
}

// 无后端联调时的本地 mock,经 VITE_USE_AUTH_MOCK=true 开启
function mockLogin({ username }: LoginPayload): LoginResult {
  const platform: Platform = username.toLowerCase().includes('admin') ? 'admin' : 'tenant'
  return { platform, accessToken: `mock-${platform}-token` }
}
