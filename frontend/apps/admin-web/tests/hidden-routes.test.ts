import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@repo/hooks', () => ({
  getToken: vi.fn(() => ''),
  setToken: vi.fn(),
  clearToken: vi.fn(),
}))
vi.mock('@/api/auth', () => ({
  getUserInfo: vi.fn(),
  getMenus: vi.fn(),
}))

import { getMenus, getUserInfo } from '@/api/auth'
import { useAuthStore } from '@/store/auth'
import { HIDDEN_ROUTES } from '@/router/hidden'

/** /auth/menus 字典形态:无权限的菜单不进 dict,enabled=false 表示被开关下线 */
const mockProfile = (menus: Record<string, { enabled: boolean }>) => {
  vi.mocked(getUserInfo).mockResolvedValue({
    user_id: 1,
    username: 'admin',
    tenant_id: 1,
    role_ids: [1],
    access_domain: 'platform',
    is_superuser: true,
  })
  vi.mocked(getMenus).mockResolvedValue(menus)
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
})

describe('前端隐藏路由名单(HIDDEN_ROUTES)', () => {
  it('当前隐藏名单:套餐管理/订单管理/费用告警/SSO配置/Webhook配置', () => {
    expect(HIDDEN_ROUTES).toEqual(['package', 'orders', 'alarm', 'sso', 'webhook'])
  })

  it('后端菜单权限包含隐藏路由时,hasMenu 仍返回 false(菜单/路由/全局搜索统一不可见)', async () => {
    mockProfile({ '/admin/package': { enabled: true }, '/admin/deploy': { enabled: true } })
    const auth = useAuthStore()
    await auth.fetchProfile()
    expect(auth.hasMenu('package')).toBe(false) // 有权限但被前端隐藏
    expect(auth.hasMenu('deploy')).toBe(true) // 非隐藏路由不受影响
  })

  it('五个隐藏路由即使后端返回权限也一律不可见', async () => {
    mockProfile(Object.fromEntries(HIDDEN_ROUTES.map((name) => [`/admin/${name}`, { enabled: true }])))
    const auth = useAuthStore()
    await auth.fetchProfile()
    for (const name of HIDDEN_ROUTES) expect(auth.hasMenu(name)).toBe(false)
  })

  it('firstMenuName 跳过隐藏路由,取第一个可见菜单(guard 重定向不落隐藏页)', async () => {
    mockProfile({
      '/admin/package': { enabled: true },
      '/admin/sso': { enabled: true },
      '/admin/dashboard': { enabled: true },
    })
    const auth = useAuthStore()
    await auth.fetchProfile()
    expect(auth.firstMenuName()).toBe('dashboard')
  })

  it('全部菜单均被隐藏时,firstMenuName 返回空字符串', async () => {
    mockProfile({ '/admin/package': { enabled: true }, '/admin/alarm': { enabled: true } })
    const auth = useAuthStore()
    await auth.fetchProfile()
    expect(auth.firstMenuName()).toBe('')
  })
})

describe('/auth/menus 字典形态与可见性开关', () => {
  it('key 不存在=无功能权限:hasMenu false 且 menuEntry 为 undefined(侧栏不渲染)', async () => {
    mockProfile({ '/admin/deploy': { enabled: true } })
    const auth = useAuthStore()
    await auth.fetchProfile()
    expect(auth.hasMenu('deploy')).toBe(true)
    expect(auth.hasMenu('bills')).toBe(false)
    expect(auth.menuEntry('bills')).toBeUndefined()
  })

  it('enabled=false=有权但被开关下线:hasMenu false(侧栏隐藏、路由拦截),menuEntry 保留(守卫区分下线/无权限提示)', async () => {
    mockProfile({ '/admin/alarm': { enabled: false } })
    const auth = useAuthStore()
    await auth.fetchProfile()
    expect(auth.hasMenu('alarm')).toBe(false)
    expect(auth.menuEntry('alarm')).toEqual({ enabled: false })
  })

  it('firstMenuName 跳过被下线菜单,取第一个可用菜单', async () => {
    mockProfile({
      '/admin/dashboard': { enabled: false },
      '/admin/deploy': { enabled: true },
    })
    const auth = useAuthStore()
    await auth.fetchProfile()
    expect(auth.firstMenuName()).toBe('deploy')
  })

  it('firstMenuName 按侧栏声明序取第一个可用菜单(不按字典 key 顺序)', async () => {
    // 字典 key 顺序 tenant-management 在前;侧栏声明序 dashboard 在「监控」组第一位
    mockProfile({
      '/admin/tenant-management': { enabled: true },
      '/admin/dashboard': { enabled: true },
    })
    const auth = useAuthStore()
    await auth.fetchProfile()
    expect(auth.firstMenuName()).toBe('dashboard')

    // 侧栏序第一项被下线后,顺延到侧栏序下一个可用菜单
    await auth.applyDisabled(['/admin/dashboard'])
    expect(auth.firstMenuName()).toBe('tenant-management')
  })

  it('公共页(about)不在菜单字典内也恒可用', async () => {
    mockProfile({})
    const auth = useAuthStore()
    await auth.fetchProfile()
    expect(auth.hasMenu('about')).toBe(true)
  })
})
