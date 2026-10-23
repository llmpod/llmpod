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
import type { LlmpodSettingsSeed } from '@repo/hooks'
import * as authModule from '@/store/auth'
import { START_LOCATION, type Router } from 'vue-router'
import { QUICK_SETUP_DONE_KEY, QUICK_SETUP_PROMPT_KEY, QUICK_SETUP_SKIP_KEY } from '@/api/quick-setup'
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
type GuardFn = (to: GuardTo, from?: unknown) => unknown

function captureGuard(): GuardFn {
  let guard: GuardFn | null = null
  setupGuard({ beforeEach: (fn: GuardFn) => { guard = fn } } as unknown as Router)
  return guard!
}

/** 注入变量种子（守卫只读 first_guide，其余设置字段与本组用例无关） */
function seedSettings(firstGuide: boolean): void {
  window.__LLMPOD_SETTINGS__ = {
    maintenanceMode: false,
    first_guide: firstGuide,
  } as LlmpodSettingsSeed
}

beforeEach(() => {
  vi.clearAllMocks()
  auth.token = ''
  auth.userInfo = null
  auth.menus = {}
  auth.hasMenu.mockReturnValue(false)
  auth.firstMenuName.mockReturnValue('')
  sessionStorage.clear()
  window.__LLMPOD_SETTINGS__ = undefined
})

describe('admin router guard', () => {
  it('public 路由直接放行(不看登录态)', async () => {
    const guard = captureGuard()
    await expect(guard({ meta: { public: true } })).resolves.toBe(true)
  })

  it('未登录访问受保护页:整页跳 portal 登录并中断', async () => {
    const guard = captureGuard()
    await expect(guard({ name: 'deploy', meta: {} })).resolves.toBe(false)
    expect(goToPortalLogin).toHaveBeenCalledTimes(1)
    expect(auth.fetchProfile).not.toHaveBeenCalled()
  })

  it('已登录首次进入:拉取用户信息与菜单', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.menus = { '/admin/deploy': { enabled: true } }
    auth.hasMenu.mockReturnValue(true)
    await expect(guard({ name: 'deploy', meta: {} })).resolves.toBe(true)
    expect(auth.fetchProfile).toHaveBeenCalledTimes(1)
  })

  it('已有 userInfo 不重复拉取', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/deploy': { enabled: true } }
    auth.hasMenu.mockReturnValue(true)
    await expect(guard({ name: 'deploy', meta: {} })).resolves.toBe(true)
    expect(auth.fetchProfile).not.toHaveBeenCalled()
  })

  it('quick-setup 向导页不参与菜单鉴权,随时可进(e4d1d51)', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.hasMenu.mockReturnValue(false) // 无菜单权限也能进
    await expect(guard({ name: 'quick-setup', meta: {} })).resolves.toBe(true)
    expect(message.error).not.toHaveBeenCalled()
  })

  it('first_guide=true 且登录落地:放行默认页并写握手标记(BasicLayout 据此弹引导窗)', async () => {
    seedSettings(true)
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/dashboard': { enabled: true } }
    auth.hasMenu.mockReturnValue(true)
    // from=START_LOCATION 模拟登录后整页跳 /admin/dashboard 的初始导航
    await expect(guard({ name: 'dashboard', meta: {} }, START_LOCATION)).resolves.toBe(true)
    expect(sessionStorage.getItem(QUICK_SETUP_PROMPT_KEY)).toBe('1')
  })

  it('first_guide=true 但站内跳转(非落地导航)不写握手标记', async () => {
    seedSettings(true)
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/deploy': { enabled: true } }
    auth.hasMenu.mockReturnValue(true)
    // from 为普通路由:跳过引导,直接放行
    await expect(guard({ name: 'deploy', meta: {} }, { name: 'dashboard' })).resolves.toBe(true)
    expect(sessionStorage.getItem(QUICK_SETUP_PROMPT_KEY)).toBeNull()
  })

  it('skip/done 会话标记压制 first_guide 引导(不写握手标记,防重复弹)', async () => {
    seedSettings(true)
    sessionStorage.setItem(QUICK_SETUP_SKIP_KEY, '1')
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/dashboard': { enabled: true } }
    auth.hasMenu.mockReturnValue(true)
    await expect(guard({ name: 'dashboard', meta: {} }, START_LOCATION)).resolves.toBe(true)
    expect(sessionStorage.getItem(QUICK_SETUP_PROMPT_KEY)).toBeNull()

    sessionStorage.removeItem(QUICK_SETUP_SKIP_KEY)
    sessionStorage.setItem(QUICK_SETUP_DONE_KEY, '1')
    await expect(guard({ name: 'dashboard', meta: {} }, START_LOCATION)).resolves.toBe(true)
    expect(sessionStorage.getItem(QUICK_SETUP_PROMPT_KEY)).toBeNull()
  })

  it('first_guide 未注入/为 false:落地导航直接放行', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/dashboard': { enabled: true } }
    auth.hasMenu.mockReturnValue(true)
    seedSettings(false)
    await expect(guard({ name: 'dashboard', meta: {} }, START_LOCATION)).resolves.toBe(true)
    expect(sessionStorage.getItem(QUICK_SETUP_PROMPT_KEY)).toBeNull()
    // 注入变量整体缺失（后端 DB 读失败未注入）同样不引导
    window.__LLMPOD_SETTINGS__ = undefined
    await expect(guard({ name: 'dashboard', meta: {} }, START_LOCATION)).resolves.toBe(true)
    expect(sessionStorage.getItem(QUICK_SETUP_PROMPT_KEY)).toBeNull()
  })

  it('守卫不再做集群探测与自动引导:有权限页直接放行,不请求集群接口', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/deploy': { enabled: true } }
    auth.hasMenu.mockReturnValue(true)
    // 不设置任何 quick-setup 跳过标记,模拟空集群环境,仍应直接放行
    await expect(guard({ name: 'deploy', meta: {} })).resolves.toBe(true)
  })

  it('无 name 路由(重定向)放行', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    await expect(guard({ name: null, meta: {} })).resolves.toBe(true)
  })

  it('拉取菜单后一个权限都没有:清 token 提示并整页回登录页', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    await expect(guard({ name: 'deploy', meta: {} })).resolves.toBe(false)
    expect(auth.clearToken).toHaveBeenCalledTimes(1)
    expect(message.warning).toHaveBeenCalledWith('账号无任何菜单权限,请重新登录')
    expect(goToPortalLogin).toHaveBeenCalledTimes(1)
    expect(message.error).not.toHaveBeenCalled()
  })

  it('无菜单权限:提示并落到第一个有权限菜单', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/dashboard': { enabled: true } }
    auth.hasMenu.mockReturnValue(false)
    auth.firstMenuName.mockReturnValue('dashboard')
    await expect(guard({ name: 'deploy', meta: {} })).resolves.toEqual({ name: 'dashboard' })
    expect(message.error).toHaveBeenCalledWith('无操作权限')
  })

  it('模块被本人自助关闭(如登录落地页已隐藏):静默换到第一个可用菜单,不弹错误', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/dashboard': { enabled: false }, '/admin/deploy': { enabled: true } }
    auth.hasMenu.mockReturnValue(false)
    auth.menuEntry.mockReturnValueOnce({ enabled: false })
    auth.firstMenuName.mockReturnValue('deploy')
    await expect(guard({ name: 'dashboard', meta: {} })).resolves.toEqual({ name: 'deploy' })
    expect(message.error).not.toHaveBeenCalled()
  })

  it('无权限且首菜单为空/首菜单即当前页:中断不跳转', async () => {
    const guard = captureGuard()
    auth.token = 'tok'
    auth.userInfo = { username: 'admin' }
    auth.menus = { '/admin/dashboard': { enabled: true } }
    auth.hasMenu.mockReturnValue(false)
    auth.firstMenuName.mockReturnValue('')
    await expect(guard({ name: 'deploy', meta: {} })).resolves.toBe(false)

    auth.firstMenuName.mockReturnValue('deploy') // 首菜单即当前页,防循环
    await expect(guard({ name: 'deploy', meta: {} })).resolves.toBe(false)
  })
})
