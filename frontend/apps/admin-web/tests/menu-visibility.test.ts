// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/auth', () => ({
  getMenus: vi.fn(),
  updateMyMenus: vi.fn(),
}))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return { ...actual, message: { success: vi.fn(), error: vi.fn() } }
})

import { message } from '@repo/hooks'
import { getMenus, updateMyMenus } from '@/api/auth'
import { useAuthStore } from '@/store/auth'
import MenuVisibility from '@/views/config/components/MenuVisibility.vue'

/**
 * /auth/menus 字典 fixture:key=侧栏路由路径(存在=有权限),含
 * - /admin/model-mark:本人已关闭(enabled=false)
 * - /admin/config:设置页自身(应锁定不可关)
 * - /admin/alarm:前端隐藏路由(有权限但侧栏无入口,不应进自助开关列表)
 */
function makeDict() {
  return {
    '/admin/dashboard': { enabled: true },
    '/admin/model-mark': { enabled: false },
    '/admin/config': { enabled: true },
    '/admin/alarm': { enabled: true },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
})

/** 挂载组件;store 先回填字典模拟登录后的侧栏状态,便于断言保存后即时生效 */
function mountPage() {
  const auth = useAuthStore()
  auth.menus = makeDict()
  return mount(MenuVisibility)
}

describe('设置页「模块管理」tab(本人自助 GET/POST /auth/menus)', () => {
  it('回显:字典 ∩ 侧栏声明,隐藏路由不进列表,已关闭项幽灵化,无保存按钮', async () => {
    vi.mocked(getMenus).mockResolvedValue(makeDict())
    const wrapper = mountPage()
    await vi.waitFor(() => expect(wrapper.text()).toContain('总览大屏'))
    expect(wrapper.text()).toContain('/admin/dashboard')
    // 前端隐藏路由(费用告警):侧栏无入口,不出现在自助开关里
    expect(wrapper.text()).not.toContain('费用告警')
    // 已关闭项不再渲染「已隐藏」标签:状态由开关关闭+卡片幽灵化表达
    expect(wrapper.findAll('.n-tag')).toHaveLength(0)
    // 开关即改即存,页面上没有保存按钮(n-switch 渲染为 div,button 只可能来自 n-button)
    expect(wrapper.findAll('button')).toHaveLength(0)
    // 行序即侧栏声明序:总览大屏(开)/模型市场(关)/设置(开)
    const switches = wrapper.findAll('.n-switch')
    expect(switches).toHaveLength(3)
    expect(switches[0].classes()).toContain('n-switch--active')
    expect(switches[1].classes()).not.toContain('n-switch--active')
    wrapper.unmount()
  })

  it('「设置」行锁定不可关:开关禁用,悬浮才经 tooltip 提示,点击不发起保存(防自锁)', async () => {
    vi.mocked(getMenus).mockResolvedValue(makeDict())
    const wrapper = mountPage()
    await vi.waitFor(() => expect(wrapper.text()).toContain('总览大屏'))
    const switches = wrapper.findAll('.n-switch')
    expect(switches[2].classes()).toContain('n-switch--disabled')
    // 提示不常驻卡片,悬浮锁定开关时才弹出(tooltip 渲染在 body)
    expect(wrapper.text()).not.toContain('设置入口,不可关闭')
    await switches[2].trigger('mouseenter')
    await vi.waitFor(() => expect(document.body.textContent).toContain('设置入口,不可关闭'))
    await switches[2].trigger('click')
    expect(updateMyMenus).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('重新开启「模型市场」:立即全量提交空列表,成功后回显并即时写回侧栏字典', async () => {
    vi.mocked(getMenus).mockResolvedValue(makeDict())
    vi.mocked(updateMyMenus).mockResolvedValue({ user_id: 1, disabled: [] })
    const wrapper = mountPage()
    await vi.waitFor(() => expect(wrapper.text()).toContain('总览大屏'))
    await wrapper.findAll('.n-switch')[1].trigger('click')
    // 保存成功不弹提示(8c6d847 移除),等待保存完成以开关回显为准
    await vi.waitFor(() =>
      expect(wrapper.findAll('.n-switch')[1].classes()).toContain('n-switch--active'),
    )
    // 全量覆盖:其它行全开,disabled 即空数组(空数组=全部显示)
    expect(vi.mocked(updateMyMenus).mock.calls[0][0]).toEqual([])
    // 成功后开关回显为开
    expect(wrapper.findAll('.n-switch')[1].classes()).toContain('n-switch--active')
    // 保存后即时写回 store:侧栏无需刷新立即生效
    expect(useAuthStore().menus['/admin/model-mark']).toEqual({ enabled: true })
    wrapper.unmount()
  })

  it('关闭「总览大屏」:全量提交含原本已关闭的「模型市场」,成功后写回字典', async () => {
    vi.mocked(getMenus).mockResolvedValue(makeDict())
    vi.mocked(updateMyMenus).mockResolvedValue({
      user_id: 1,
      disabled: ['/admin/model-mark', '/admin/dashboard'],
    })
    const wrapper = mountPage()
    await vi.waitFor(() => expect(wrapper.text()).toContain('总览大屏'))
    await wrapper.findAll('.n-switch')[0].trigger('click')
    // 保存成功不弹提示(8c6d847 移除),等待保存完成以字典写回为准
    await vi.waitFor(() =>
      expect(useAuthStore().menus['/admin/dashboard']).toEqual({ enabled: false }),
    )
    expect(vi.mocked(updateMyMenus).mock.calls[0][0]).toEqual(['/admin/dashboard', '/admin/model-mark'])
    expect(wrapper.findAll('.n-switch')[0].classes()).not.toContain('n-switch--active')
    expect(useAuthStore().menus['/admin/dashboard']).toEqual({ enabled: false })
    wrapper.unmount()
  })

  it('保存失败(422 非法路径等):提示由 http 层弹出,该行回滚,字典不动', async () => {
    vi.mocked(getMenus).mockResolvedValue(makeDict())
    vi.mocked(updateMyMenus).mockRejectedValue(new Error('path 非法'))
    const wrapper = mountPage()
    await vi.waitFor(() => expect(wrapper.text()).toContain('总览大屏'))
    // 开启「模型市场」(乐观更新后保存失败)
    await wrapper.findAll('.n-switch')[1].trigger('click')
    await vi.waitFor(() => expect(updateMyMenus).toHaveBeenCalledTimes(1))
    expect(message.success).not.toHaveBeenCalled()
    // 失败回滚:「模型市场」回到关闭态,store 字典保持服务端态
    await vi.waitFor(() =>
      expect(wrapper.findAll('.n-switch')[1].classes()).not.toContain('n-switch--active'),
    )
    expect(useAuthStore().menus['/admin/model-mark']).toEqual({ enabled: false })
    wrapper.unmount()
  })

  it('保存串行:上一次保存未完成时其它开关切换被忽略,当前行开关转圈', async () => {
    vi.mocked(getMenus).mockResolvedValue(makeDict())
    let resolveSave!: (state: { user_id: number; disabled: string[] }) => void
    vi.mocked(updateMyMenus).mockImplementation(
      () => new Promise<{ user_id: number; disabled: string[] }>((resolve) => { resolveSave = resolve }),
    )
    const wrapper = mountPage()
    await vi.waitFor(() => expect(wrapper.text()).toContain('总览大屏'))
    const switches = wrapper.findAll('.n-switch')
    // 点「总览大屏」(开→关),保存挂起
    await switches[0].trigger('click')
    await vi.waitFor(() => expect(switches[0].classes()).toContain('n-switch--loading'))
    // 保存未完成时点「模型市场」:被忽略,不发起第二次保存
    await switches[1].trigger('click')
    expect(updateMyMenus).toHaveBeenCalledTimes(1)
    expect(switches[1].classes()).not.toContain('n-switch--active')
    // 挂起的保存完成后回到空闲,行按返回体回显(成功不弹提示,以 loading 消失为准)
    resolveSave({ user_id: 1, disabled: ['/admin/dashboard', '/admin/model-mark'] })
    await vi.waitFor(() => expect(switches[0].classes()).not.toContain('n-switch--loading'))
    expect(switches[0].classes()).not.toContain('n-switch--active')
    wrapper.unmount()
  })

  it('字典为空或全被过滤:展示空态', async () => {
    vi.mocked(getMenus).mockResolvedValue({ '/admin/alarm': { enabled: true } })
    const wrapper = mountPage()
    await vi.waitFor(() => expect(wrapper.text()).toContain('暂无可配置的菜单'))
    wrapper.unmount()
  })
})