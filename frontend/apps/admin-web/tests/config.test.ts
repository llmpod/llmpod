// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
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
    useDialog: vi.fn(),
  }
})

import { message, useBrand, useConfirm, useDialog } from '@repo/hooks'
import { getAdminSettings, updateAdminSettings } from '@/api/settings'
import { useSettingsStore } from '@/store/settings'
import { routes } from '@/router/routes'
import { useController } from '@/views/config/useController'
import TelemetrySettings from '@/views/config/components/TelemetrySettings.vue'
import { searchTabs } from '@/views/config/tabs'

/** 回显 fixture:通用组有值,维护关闭,遥测默认开启 */
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
  telemetryEnabled: true,
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

/** 挂载遥测卡片(自治组件,不走 controller) */
function mountTelemetry() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(TelemetrySettings, {
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  return { wrapper, store: useSettingsStore(pinia) }
}

/** 开关状态(取 naive NSwitch 的 aria-checked) */
function switchChecked(wrapper: VueWrapper) {
  return wrapper.find('[role="switch"]').attributes('aria-checked')
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

describe('config 用户体验改进计划', () => {
  it('单开关布局:无保存按钮,退出先二次确认再提交 {telemetryEnabled:false}', async () => {
    const { wrapper, store } = mountTelemetry()
    await waitForSettingsLoaded(store)
    await nextTick()

    // 结构就是一个开关 + label 后的问号图标,没有保存按钮
    expect(wrapper.findAll('.n-switch')).toHaveLength(1)
    expect(wrapper.findAll('button').filter((btn) => btn.text().includes('保存'))).toHaveLength(0)
    expect(wrapper.find('[aria-label="用户体验改进计划说明"]').exists()).toBe(true)
    expect(switchChecked(wrapper)).toBe('true')

    await wrapper.find('.n-switch').trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已退出用户体验改进计划'))

    // 退出是用户可感知的行为变更:先确认,确认后只提交开关一键
    expect(useConfirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: '退出用户体验改进计划' }),
    )
    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toEqual({ telemetryEnabled: false })
    expect(updateAdminSettings).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('退出确认取消:不发请求,开关保持开启', async () => {
    vi.mocked(useConfirm).mockRejectedValueOnce(new Error('cancel'))
    const { wrapper, store } = mountTelemetry()
    await waitForSettingsLoaded(store)
    await nextTick()

    await wrapper.find('.n-switch').trigger('click')
    await flushPromises()

    expect(useConfirm).toHaveBeenCalled()
    expect(updateAdminSettings).not.toHaveBeenCalled()
    expect(switchChecked(wrapper)).toBe('true')
    wrapper.unmount()
  })

  it('开启计划:不弹二次确认,直接提交', async () => {
    settingsRow = { ...settingsRow, telemetryEnabled: false }
    const { wrapper, store } = mountTelemetry()
    await waitForSettingsLoaded(store)
    await nextTick()
    expect(switchChecked(wrapper)).toBe('false')

    await wrapper.find('.n-switch').trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('已加入用户体验改进计划'))

    expect(useConfirm).not.toHaveBeenCalled()
    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toEqual({ telemetryEnabled: true })
    wrapper.unmount()
  })

  it('回显缺省:后端未下发 telemetryEnabled 时开关默认开启', async () => {
    // 构造旧版后端/旧缓存形状:回显不含 telemetryEnabled 键
    settingsRow = { ...settingsRow }
    delete (settingsRow as { telemetryEnabled?: boolean }).telemetryEnabled
    const { wrapper, store } = mountTelemetry()
    await waitForSettingsLoaded(store)
    await nextTick()

    expect(switchChecked(wrapper)).toBe('true')
    wrapper.unmount()
  })

  it('保存失败:开关回滚到切换前,不留「界面已关/后端未改」的偏差', async () => {
    vi.mocked(updateAdminSettings).mockRejectedValueOnce(new Error('boom'))
    const { wrapper, store } = mountTelemetry()
    await waitForSettingsLoaded(store)
    await nextTick()

    await wrapper.find('.n-switch').trigger('click')
    await flushPromises()

    await vi.waitFor(() => expect(switchChecked(wrapper)).toBe('true'))
    expect(message.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('点击问号图标新窗口打开说明文档,不再弹窗', async () => {
    const { wrapper, store } = mountTelemetry()
    await waitForSettingsLoaded(store)
    await nextTick()

    // 说明文案已迁到文档站:图标是外链,a11y 文案沿用原名
    const link = wrapper.find('[aria-label="用户体验改进计划说明"]')
    expect(link.element.tagName).toBe('A')
    expect(link.attributes('href')).toBe('https://docs.llmpod.ai/telemetry')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    // 跳转不占用设置区:不弹窗,也不触发保存
    expect(useDialog).not.toHaveBeenCalled()
    expect(updateAdminSettings).not.toHaveBeenCalled()
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
