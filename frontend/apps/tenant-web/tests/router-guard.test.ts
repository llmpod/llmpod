// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@repo/hooks', () => ({
  goToPortalLogin: vi.fn(),
  message: { error: vi.fn(), warning: vi.fn() },
}))
vi.mock('@/store/auth', () => {
  const auth = {
    token: '',
    userInfo: null as unknown,
    menus: {} as Record<string, unknown>,
    fetchProfile: vi.fn(async () => {}),
    hasMenu: vi.fn(() => false),
    menuEntry: vi.fn(() => undefined),
    firstMenuName: vi.fn(() => ''),
    clearToken: vi.fn(),
  }
  return { useAuthStore: () => auth, __auth: auth }
})

import { goToPortalLogin, message } from '@repo/hooks'
import * as authModule from '@/store/auth'
import type { Router } from 'vue-router'
import { setupGuard } from '@/router/guard'

const auth = (authModule as unknown as { __auth: {
  token: string
  userInfo: unknown
  menus: Record<string, unknown>
  fetchProfile: ReturnType<typeof vi.fn>
  hasMenu: ReturnType<typeof vi.fn>
  menuEntry: ReturnType<typeof vi.fn>
  firstMenuName: ReturnType<typeof vi.fn>
  clearToken: ReturnType<typeof vi.fn>
} }).__auth

type GuardTo = { name?: string | null; meta: Record<string, unknown> }
type GuardFn = (to: GuardTo) => unknown

function captureGuard(): GuardFn {
  let guard: GuardFn | null = null
  setupGuard({ beforeEach: (fn: GuardFn) => { guard = fn } } as unknown as Router)
  return guard!
}

beforeEach(() => {
  vi.clearAllMocks()
  auth.token = ''
  auth.userInfo = null
  auth.menus = {}
  auth.hasMenu.mockReturnValue(false)
  auth.firstMenuName.mockReturnValue('')
})

describe('tenant router guard', () => {
  it('public 路由直接放行(不看登录态)', async () => {
    const guard = captureGuard()
    await expect(guard({ meta: { public: true } })).resolves.toBe(true)
  })

  it('未登录访问受保护页:整页跳 portal 登录并中断', async () => {
    const guard = captureGuard()
    await expect(guard({ name: 'console', meta: {} })).resolves.toBe(false)
    expect(goToPortalLogin).toHaveBeenCalledTimes(1)
    expect(auth.fetchProfile).not.toHaveBeenCalled()
  })

  it('已登录首次进入:拉取用户信息与菜单', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.menus = { '/tenant/console': { enabled: true } }
    auth.hasMenu.mockReturnValue(true)
    await expect(guard({ name: 'console', meta: {} })).resolves.toBe(true)
    expect(auth.fetchProfile).toHaveBeenCalledTimes(1)
  })

  it('拉取菜单后一个权限都没有:清 token 提示并整页回登录页', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    await expect(guard({ name: 'console', meta: {} })).resolves.toBe(false)
    expect(auth.clearToken).toHaveBeenCalledTimes(1)
    expect(message.warning).toHaveBeenCalledWith('账号无任何菜单权限,请重新登录')
    expect(goToPortalLogin).toHaveBeenCalledTimes(1)
    expect(message.error).not.toHaveBeenCalled()
  })

  it('无菜单权限:提示并落到第一个有权限菜单', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'user' }
    auth.menus = { '/tenant/team': { enabled: true } }
    auth.hasMenu.mockReturnValue(false)
    auth.firstMenuName.mockReturnValue('team')
    await expect(guard({ name: 'console', meta: {} })).resolves.toEqual({ name: 'team' })
    expect(message.error).toHaveBeenCalledWith('无操作权限')
  })

  it('菜单被本人自助关闭(如登录落地页已隐藏):静默换到第一个可用菜单,不弹错误', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'user' }
    auth.menus = { '/tenant/console': { enabled: false }, '/tenant/team': { enabled: true } }
    auth.hasMenu.mockReturnValue(false)
    auth.menuEntry.mockReturnValueOnce({ enabled: false })
    auth.firstMenuName.mockReturnValue('team')
    await expect(guard({ name: 'console', meta: {} })).resolves.toEqual({ name: 'team' })
    expect(message.error).not.toHaveBeenCalled()
  })

  it('无权限且首菜单为空/首菜单即当前页:中断不跳转', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'user' }
    auth.menus = { '/tenant/team': { enabled: true } }
    auth.hasMenu.mockReturnValue(false)
    auth.firstMenuName.mockReturnValue('')
    await expect(guard({ name: 'console', meta: {} })).resolves.toBe(false)

    auth.firstMenuName.mockReturnValue('console') // 首菜单即当前页,防循环
    await expect(guard({ name: 'console', meta: {} })).resolves.toBe(false)
  })
})
