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
    message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
  }
})

import { DEFAULT_BRAND_CONFIG, message, useBrand, useTheme } from '@repo/hooks'
import { getAdminSettings, updateAdminSettings } from '@/api/settings'
import { useSettingsStore } from '@/store/settings'
import { useController } from '@/views/brand/useController'

/** 回显 fixture:品牌组全配置,登录页组未配置(全 null) */
const SETTINGS_FIXTURE = {
  siteName: 'LLMPod 平台',
  siteDescription: null,
  platformName: 'LLMPod',
  logo: '🚀',
  footerText: '© 2026 LLMPod',
  themeColor: '#1261a8',
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
  localStorage.clear()
  // 模拟 index.html 静态标题初始态(三端统一 LLMPod)
  document.title = 'LLMPod'
  settingsRow = { ...SETTINGS_FIXTURE }
  vi.mocked(getAdminSettings).mockImplementation(async () => ({ ...settingsRow }))
  vi.mocked(updateAdminSettings).mockImplementation(async (data) => {
    settingsRow = { ...settingsRow, ...data }
    return { ...settingsRow }
  })
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

/** 等回显单例就绪并 flush watch 回填,避免操作值被回显覆盖 */
async function waitForSettingsLoaded(store: ReturnType<typeof useSettingsStore>) {
  await vi.waitFor(() => expect(store.settings).not.toBeNull())
  await nextTick()
}

function findInputByPlaceholder(form: ReturnType<typeof mount>, keyword: string) {
  return form.findAll('input').find((input) =>
    input.attributes('placeholder')?.includes(keyword),
  )!
}

describe('brand 回显', () => {
  it('load 完成后品牌字段回填表单(null → 空输入框)', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.BrandForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    // placeholder 匹配取「关于页显示默认」子串:与页脚的「未配置时显示默认页脚」区分
    const nameInput = findInputByPlaceholder(form, '关于页显示默认')
    expect((nameInput.element as HTMLInputElement).value).toBe('LLMPod')
    expect((findInputByPlaceholder(form, 'Emoji/URL').element as HTMLInputElement).value).toBe('🚀')
    wrapper.unmount()
  })

  it('登录页组未配置:仅背景图输入框为空,不残留旧字段名', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LoginPageForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    // 「登录页背景色」入口已下线:表单只剩背景图 URL 一个输入框(此前 [0] 是主色 hex)
    const inputs = form.findAll('input')
    expect(inputs).toHaveLength(1)
    expect(inputs[0].attributes('placeholder')).toContain('http')
    expect((inputs[0].element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })
})

describe('brand 保存', () => {
  it('保存品牌组:仅品牌四键 payload + 响应喂全局状态立即生效', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.BrandForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    const nameInput = findInputByPlaceholder(form, '关于页显示默认')
    expect((nameInput.element as HTMLInputElement).value).toBe('LLMPod')
    // 改为自定义名保存,验证标题跟随表单值(与默认回落值区分)
    await nameInput.setValue('Acme 平台')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存配置'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('配置已保存'))

    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toEqual({
      platformName: 'Acme 平台',
      logo: '🚀',
      themeColor: '#1261a8',
      footerText: '© 2026 LLMPod',
    })
    // 保存后立即生效:响应 data 喂 setThemeConfig/setBrandConfig
    expect(useTheme().themeConfig.value.primary).toBe('#1261a8')
    expect(useBrand().brandConfig.value.platformName).toBe('Acme 平台')
    expect(useBrand().brandConfig.value.footerText).toBe('© 2026 LLMPod')
    // 网页标题同步覆盖:siteName(通用设置)优先于 platformName(fixture 站点名为「LLMPod 平台」)
    expect(document.title).toBe('LLMPod 平台')
    wrapper.unmount()
  })

  it('保存登录页组:背景色不在表单内仍原样透传,背景图落库并喂 remoteTheme 缓存', async () => {
    // 服务端已配置背景色:隐藏入口后保存背景图不得把它覆盖为默认/空
    settingsRow = { ...settingsRow, loginPrimary: '#0ea5e9' }
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LoginPageForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    await form.findAll('input')[0].setValue('https://cdn.example.com/bg.png')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存配置'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('配置已保存'))

    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toEqual({
      loginPrimary: '#0ea5e9',
      loginBackground: 'https://cdn.example.com/bg.png',
    })
    // 管理员自己立即生效:缓存同步更新(下次刷新 theme.boot 桥接同源)
    expect(JSON.parse(localStorage.getItem('app_remote_theme') ?? '{}')).toMatchObject({
      loginPrimary: '#0ea5e9',
      loginBackground: 'https://cdn.example.com/bg.png',
    })
    wrapper.unmount()
  })

  it('登录页组清空:空输入框提交传 null(三态清空),缓存回落', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LoginPageForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存配置'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('配置已保存'))

    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toEqual({
      loginPrimary: null,
      loginBackground: null,
    })
    // 清空喂空串覆盖缓存:computed 回落全站 primary
    expect(JSON.parse(localStorage.getItem('app_remote_theme') ?? '{}').loginPrimary).toBe('')
    wrapper.unmount()
  })

  it('平台名称可选:清空后提交传 null 回落默认,不拦截保存', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.BrandForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    const nameInput = findInputByPlaceholder(form, '关于页显示默认')
    expect((nameInput.element as HTMLInputElement).value).toBe('LLMPod')
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存配置'))!

    // 前置:先保存自定义名,让全局 brandConfig 持有非默认值(fixture 默认名恰为 LLMPod,不前置无法暴露残留)
    await nameInput.setValue('Acme 平台')
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('配置已保存'))
    expect(useBrand().brandConfig.value.platformName).toBe('Acme 平台')

    // 再清空平台名与页脚文案保存:空串转 null = 清空回落默认
    await nameInput.setValue('')
    await form.findAll('input')[3].setValue('')
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledTimes(2))

    expect(vi.mocked(updateAdminSettings).mock.calls[1][0]).toEqual({
      platformName: null,
      logo: '🚀',
      themeColor: '#1261a8',
      footerText: null,
    })
    // 清空立即生效:全局 brandConfig 回落默认名/默认页脚,侧栏标题与关于页不必刷新
    expect(useBrand().brandConfig.value.platformName).toBe(DEFAULT_BRAND_CONFIG.platformName)
    expect(useBrand().brandConfig.value.footerText).toBe(DEFAULT_BRAND_CONFIG.footerText)
    // 标题仍由 siteName(通用设置)优先控制,不回落
    expect(document.title).toBe('LLMPod 平台')
    wrapper.unmount()
  })

  it('主题色逐字输入 #f1f1f1:非法中间态不触发 seemly 抛错,终值不回滚', async () => {
    // 背景色入口下线后,共用渲染器的颜色字段只剩品牌「主题色」
    const errors: unknown[] = []
    const { controller, wrapper } = mountController()
    const form = mount(controller.BrandForm, {
      attachTo: document.body,
      global: { config: { errorHandler(err: unknown) { errors.push(err) } } },
    })

    // input 序列:平台名称/logo/主题色 hex/页脚文案
    const hexInput = form.findAll('input')[2]
    await vi.waitFor(() => expect((hexInput.element as HTMLInputElement).value).toBe('#1261a8'))

    // 模拟逐字符键入:#/#f/#f1/#f1f/#f1f1/#f1f1f/#f1f1f1
    // 无守卫时非法中间态传入 NColorPicker 会让 seemly.rgba 抛错打断渲染管线(值回滚)
    const final = '#f1f1f1'
    for (let i = 1; i <= final.length; i++) {
      await hexInput.setValue(final.slice(0, i))
      await new Promise((resolve) => setTimeout(resolve, 0))
    }

    expect(errors).toEqual([])
    expect((hexInput.element as HTMLInputElement).value).toBe('#f1f1f1')
    wrapper.unmount()
  })
})

/** 在 input 上派发带 clipboardData 的 paste 事件(冒泡到 NInput 根元素上的 onPaste 拦截器) */
function dispatchPaste(input: HTMLInputElement, text: string) {
  const event = new Event('paste', { bubbles: true, cancelable: true }) as ClipboardEvent
  Object.defineProperty(event, 'clipboardData', {
    value: { getData: (type: string) => (type === 'text' ? text : '') },
  })
  input.dispatchEvent(event)
  return event
}

describe('brand 超长粘贴防御', () => {
  it('背景图粘贴超长 base64:preventDefault 阻断插入并提示改用 URL', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LoginPageForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    const bgInput = form.findAll('input')[0]
    // 模拟截图工具产出的超大 data URI(超 100 万字符上限)
    const oversized = `data:image/png;base64,${'A'.repeat(1_000_001)}`
    const event = dispatchPaste(bgInput.element as HTMLInputElement, oversized)

    expect(event.defaultPrevented).toBe(true)
    expect((bgInput.element as HTMLInputElement).value).toBe('')
    expect(message.warning).toHaveBeenCalledTimes(1)
    expect(vi.mocked(message.warning).mock.calls[0][0]).toContain('http')
    wrapper.unmount()
  })

  it('背景图粘贴合法长度 URL:不拦截不提示', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LoginPageForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    const bgInput = form.findAll('input')[0]
    const event = dispatchPaste(
      bgInput.element as HTMLInputElement,
      'https://cdn.example.com/bg.png',
    )

    expect(event.defaultPrevented).toBe(false)
    expect(message.warning).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('背景图输入框带 maxlength 兜底(防拖放/autofill 等非粘贴路径)', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LoginPageForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    expect(form.findAll('input')[0].attributes('maxlength')).toBe('1000000')
    wrapper.unmount()
  })

  it('logo 粘贴超长内容:同样被拦截', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.BrandForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    const logoInput = findInputByPlaceholder(form, 'Emoji/URL')
    const event = dispatchPaste(
      logoInput.element as HTMLInputElement,
      `data:image/png;base64,${'B'.repeat(2_000_000)}`,
    )

    expect(event.defaultPrevented).toBe(true)
    expect(message.warning).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})

describe('brand 重置默认', () => {
  it('品牌组重置:三字段清空 + 主题色回落默认色,仅重置不落库', async () => {
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.BrandForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    // 回显基础上改动,验证重置回落默认而非停留改动值
    await findInputByPlaceholder(form, '关于页显示默认').setValue('改名了')
    await form.findAll('input')[2].setValue('#0ea5e9')
    const resetBtn = form.findAll('button').find((btn) => btn.text().includes('重置默认'))!
    await resetBtn.trigger('click')

    expect((findInputByPlaceholder(form, '关于页显示默认').element as HTMLInputElement).value).toBe('')
    // input 序列:平台名称/logo/主题色 hex/页脚文案
    expect((form.findAll('input')[1].element as HTMLInputElement).value).toBe('')
    expect((form.findAll('input')[2].element as HTMLInputElement).value).toBe('#1261a8')
    // 页脚文案重置回填默认值(其余可选字段清空,保存即清空回落默认)
    expect((form.findAll('input')[3].element as HTMLInputElement).value).toBe(
      DEFAULT_BRAND_CONFIG.footerText,
    )
    expect(message.info).toHaveBeenCalledWith('已重置为默认值,保存后生效')
    expect(updateAdminSettings).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('登录页组重置:背景图清空,服务端已配置的背景色不被重置触碰', async () => {
    settingsRow = { ...settingsRow, loginPrimary: '#0ea5e9' }
    const { controller, wrapper, store } = mountController()
    const form = mount(controller.LoginPageForm, { attachTo: document.body })

    await waitForSettingsLoaded(store)
    await form.findAll('input')[0].setValue('https://cdn.example.com/bg.png')
    const resetBtn = form.findAll('button').find((btn) => btn.text().includes('重置默认'))!
    await resetBtn.trigger('click')

    expect((form.findAll('input')[0].element as HTMLInputElement).value).toBe('')
    expect(message.info).toHaveBeenCalledWith('已重置为默认值,保存后生效')
    expect(updateAdminSettings).not.toHaveBeenCalled()

    // 背景色入口已下线:重置不再回落默认深底,保存时仍原样透传服务端值
    const saveBtn = form.findAll('button').find((btn) => btn.text().includes('保存配置'))!
    await saveBtn.trigger('click')
    await vi.waitFor(() => expect(message.success).toHaveBeenCalledWith('配置已保存'))
    expect(vi.mocked(updateAdminSettings).mock.calls[0][0]).toMatchObject({
      loginPrimary: '#0ea5e9',
    })
    wrapper.unmount()
  })
})
