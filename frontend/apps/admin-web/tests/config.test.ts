// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/settings', () => ({
  getAdminSettings: vi.fn(),
  updateAdminSettings: vi.fn(),
}))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    message: { success: vi.fn(), error: vi.fn() },
    useConfirm: vi.fn(),
  }
})

import { message, useBrand, useConfirm } from '@repo/hooks'
import { getAdminSettings, updateAdminSettings } from '@/api/settings'
import { useSettingsStore } from '@/store/settings'
import { routes } from '@/router/routes'
import { useController } from '@/views/config/useController'
import { searchTabs } from '@/views/config/tabs'

/** 回显 fixture:通用组有值,维护关闭 */
const SETTINGS_FIXTURE = {
  siteName: 'LLMPod 平台',
  siteDescription: '企业级推理网关',
  platformName: null,
  logo: null,
  footerText: null,
  themeColor: null,
  mode: null,
  loginBackground: null,
  loginPrimary: null,
  maintenanceMode: false,
  maintenanceMessage: null,
  updated_by: 1,
  updated_at: '2026-09-02T00:00:00Z',
}

/** 回显行:模拟后端当前状态(POST 后 GET 返回新值),避免 save 内 load 刷回旧值 */
let settingsRow = { ...SETTINGS_FIXTURE }

beforeEach(() => {
  vi.clearAllMocks()
  // 模拟 index.html 静态标题初始态(三端统一 LLMPod)
  document.title = 'LLMPod'
  document.querySelector('meta[name="description"]')?.remove()
  localStorage.clear()
  settingsRow = { ...SETTINGS_FIXTURE }
  vi.mocked(getAdminSettings).mockImplementation(async () => ({ ...settingsRow }))
  vi.mocked(updateAdminSettings).mockImplementation(async (data) => {
    settingsRow = { ...settingsRow, ...data }
    return { ...settingsRow }
  })
  vi.mocked(useConfirm).mockResolvedValue(undefined)
})

function mountController() {
  const pinia = createPinia()
  setActivePinia(pinia)
  let controller!: ReturnType<typeof useController>
  const wrapper = mount(
    {
      setup: () => {
        controller = useController()
        return () => h('div')
      },
    },
    { global: { plugins: [pinia] } },
  )
  return { controller, wrapper, store: useSettingsStore(pinia) }
}

/** 等回显单例就绪并 flush watch 回填,避免开关/输入被回显覆盖 */
async function waitForSettingsLoaded(store: ReturnType<typeof useSettingsStore>) {
  await vi.waitFor(() => expect(store.settings).not.toBeNull())
  await nextTick()
}

describe('config 通用设置', () => {
  it('保存通用组:仅 siteName/siteDescription 两键,标题与关于页副标立即生效', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LmForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    // 回显回填完成后清空站点描述再提交(空串转 null=清空回落关于页默认副标)
    const descriptionInput = form.findAll('input')[1]
    expect((descriptionInput.element as HTMLInputElement).value).toBe('企业级推理网关')
    await descriptionInput.setValue('')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存设置'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('设置已保存'))

    const payload = vi.mocked(updateAdminSettings).mock.calls[0][0]
    expect(payload).toEqual({ siteName: 'LLMPod 平台', siteDescription: null })
    expect(payload).not.toHaveProperty('maintenanceMode')
    // 保存后立即生效:标题跟随站点名;站点描述无 SEO 作用,不再运行时写 meta 标签
    expect(document.title).toBe('LLMPod 平台')
    expect(document.querySelector('meta[name="description"]')).toBeNull()
    // 关于页副标本端立即清空(经 useBrand 共享状态,关于页回落默认文案)
    expect(useBrand().brandConfig.value.siteDescription).toBe('')
    // app_site 缓存同步写入:dev 无注入种子时,刷新后 theme.boot 读它恢复标题
    expect(JSON.parse(localStorage.getItem('app_site') ?? '{}')).toEqual({
      siteName: 'LLMPod 平台',
    })
    wrapper.unmount()
  })

  it('站点名称/描述输入框限长(100/500),超长值不发请求前端先拦', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LmForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    const [nameInput, descriptionInput] = form.findAll('input')
    expect(nameInput.attributes('maxlength')).toBe('100')
    expect(descriptionInput.attributes('maxlength')).toBe('500')
    wrapper.unmount()
  })

  it('站点名称可选:清空后提交自动保存为「LLMPod」,标题回落默认', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LmForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    const nameInput = form.findAll('input')[0]
    expect((nameInput.element as HTMLInputElement).value).toBe('LLMPod 平台')
    await nameInput.setValue('')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存设置'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('设置已保存'))

    // 空白不拦截保存,自动保存为默认「LLMPod」;描述原样提交
    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toEqual({
      siteName: 'LLMPod',
      siteDescription: '企业级推理网关',
    })
    expect(document.title).toBe('LLMPod')
    // 关于页副标同步为描述值(经 useBrand 共享状态)
    expect(useBrand().brandConfig.value.siteDescription).toBe('企业级推理网关')
    wrapper.unmount()
  })
})

describe('config 维护模式', () => {
  it('开启维护:先二次确认,确认后提交 {maintenanceMode, maintenanceMessage}', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.MaintenanceForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    // 打开维护开关(默认关闭 → 开启)
    await form.find('.n-switch').trigger('click')
    // 填维护文案
    const messageInput = form.findAll('input').find((input) =>
      input.attributes('placeholder')?.includes('系统维护中'),
    )!
    await messageInput.setValue('数据库升级,预计 30 分钟')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存维护设置'))!
    await saveBtn.trigger('click')

    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已开启维护模式'))
    // 高危操作:开启前弹二次确认
    expect(useConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '开启维护模式' }))
    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toEqual({
      maintenanceMode: true,
      maintenanceMessage: '数据库升级,预计 30 分钟',
    })
    wrapper.unmount()
  })

  it('确认取消:不提交维护设置', async () => {
    vi.mocked(useConfirm).mockRejectedValueOnce(new Error('cancel'))
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.MaintenanceForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    await form.find('.n-switch').trigger('click')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存维护设置'))!
    await saveBtn.trigger('click')

    await vi.waitFor(() => expect(useConfirm).toHaveBeenCalled())
    expect(updateAdminSettings).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('关闭维护:不弹确认直接提交', async () => {
    settingsRow = {
      ...settingsRow,
      maintenanceMode: true,
      maintenanceMessage: '数据库升级,预计 30 分钟',
    }
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.MaintenanceForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    // 回显开启态,关闭开关
    await form.find('.n-switch').trigger('click')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存维护设置'))!
    await saveBtn.trigger('click')

    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('维护设置已保存'))
    expect(useConfirm).not.toHaveBeenCalled()
    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toEqual({
      maintenanceMode: false,
      maintenanceMessage: '数据库升级,预计 30 分钟',
    })
    wrapper.unmount()
  })
})

describe('设置页子 tab 合并(通用设置 + 模块管理 + 品牌配置)', () => {
  it('tabs.ts 声明 general/menu/brand 三个子 tab,与页面页签 name 对齐', () => {
    expect(searchTabs.map((item) => item.tab)).toEqual(['general', 'menu', 'brand'])
    expect(searchTabs.map((item) => item.label)).toEqual(['通用设置', '模块管理', '品牌配置'])
  })

  it('旧 brand 路由重定向到设置页品牌配置页签,query 原样透传,搜索归属 config', () => {
    const brand = routes[0].children!.find((item) => item.name === 'brand')
    expect(brand).toBeTruthy()
    const redirect = brand!.redirect as (to: { query: Record<string, string> }) => {
      name: string
      query: Record<string, string>
    }
    expect(redirect({ query: { x: '1' } })).toEqual({ name: 'config', query: { x: '1', tab: 'brand' } })
    expect(brand!.meta?.searchGroup).toBe('config')
  })

  it('关于已独立为菜单页:config?tab=about 重定向到 about,其余 query 原样透传', () => {
    const config = routes[0].children!.find((item) => item.name === 'config')
    expect(config).toBeTruthy()
    const beforeEnter = config!.beforeEnter as (
      to: { query: Record<string, string> },
    ) => { name: string; query: Record<string, string> } | true
    expect(beforeEnter({ query: { tab: 'about', x: '1' } })).toEqual({ name: 'about', query: { x: '1' } })
    expect(beforeEnter({ query: { tab: 'brand' } })).toBe(true)
  })
})
