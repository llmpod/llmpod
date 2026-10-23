// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
// 根目录 theme.boot.js 由 vite 注入各应用 index.html,此处取源码文本在 jsdom 全局执行
// @ts-expect-error vite ?raw query import(vue-tsc 仅覆盖 src,tests 不参与类型检查)
import bootSource from '../../../theme.boot.js?raw'

/** 全局作用域执行启动脚本(等同内联 <head> 同步执行) */
function runBoot() {
  // eslint-disable-next-line no-new-func
  new Function(bootSource)()
}

/** 全配置态注入种子(与后端注入形态一致:11 字段紧凑 JSON) */
const FULL_SETTINGS = {
  siteName: 'AI 中台',
  siteDescription: '企业级推理网关',
  platformName: 'LLMPod',
  logo: '🚀',
  footerText: '© 2026 LLMPod',
  themeColor: '#1261a8',
  mode: 'auto',
  loginBackground: 'https://cdn.example.com/login.png',
  loginPrimary: '#0ea5e9',
  maintenanceMode: false,
  maintenanceMessage: null,
}

beforeEach(() => {
  localStorage.clear()
  // @ts-expect-error 测试用:重置注入种子
  window.__LLMPOD_SETTINGS__ = undefined
  // 重置网页标题,模拟各端 index.html 静态 <title> 初始态
  document.title = ''
  // 清掉 boot 动态创建的 SEO meta,避免跨用例残留
  document.querySelector('meta[name="description"]')?.remove()
})

describe('theme.boot __LLMPOD_SETTINGS__ 桥接', () => {
  it('undefined(后端读失败/dev 无注入):不动 localStorage 缓存,站点缓存兜底标题与描述', () => {
    localStorage.setItem('app_remote_theme', JSON.stringify({ primary: '#111111', mode: 'dark' }))
    localStorage.setItem('app_brand', JSON.stringify({ platformName: '旧品牌' }))
    localStorage.setItem(
      'app_site',
      JSON.stringify({ siteName: '本地站点', siteDescription: '本地描述' }),
    )

    runBoot()

    expect(JSON.parse(localStorage.getItem('app_remote_theme') ?? '{}')).toEqual({
      primary: '#111111',
      mode: 'dark',
    })
    expect(JSON.parse(localStorage.getItem('app_brand') ?? '{}')).toEqual({
      platformName: '旧品牌',
    })
    expect(JSON.parse(localStorage.getItem('app_site') ?? '{}')).toEqual({
      siteName: '本地站点',
      siteDescription: '本地描述',
    })
    // 读失败:标题与 SEO 描述从 app_site 缓存恢复,不回退默认
    expect(document.title).toBe('本地站点')
    expect(document.querySelector('meta[name="description"]')?.getAttribute('content')).toBe(
      '本地描述',
    )
  })

  it('undefined 且无站点缓存:标题与描述回落默认', () => {
    runBoot()

    expect(document.title).toBe('LLMPod')
    expect(document.querySelector('meta[name="description"]')?.getAttribute('content')).toBe(
      'LLMPod',
    )
  })

  it('null(未配置/已清空):清空三键回落默认', () => {
    localStorage.setItem('app_remote_theme', JSON.stringify({ primary: '#111111' }))
    localStorage.setItem('app_brand', JSON.stringify({ platformName: '旧品牌' }))
    localStorage.setItem('app_site', JSON.stringify({ siteName: '旧站点', siteDescription: '旧描述' }))
    window.__LLMPOD_SETTINGS__ = null

    runBoot()

    expect(localStorage.getItem('app_remote_theme')).toBeNull()
    expect(localStorage.getItem('app_brand')).toBeNull()
    expect(localStorage.getItem('app_site')).toBeNull()
    // 未配置/已清空:标题与 SEO 描述回落默认
    expect(document.title).toBe('LLMPod')
    expect(document.querySelector('meta[name="description"]')?.getAttribute('content')).toBe(
      'LLMPod',
    )
  })

  it('对象(有配置):themeColor→primary 映射重写主题缓存,品牌/站点字段重写缓存', () => {
    localStorage.setItem('app_remote_theme', JSON.stringify({ primary: '#111111' }))
    localStorage.setItem('app_brand', JSON.stringify({ platformName: '旧品牌' }))
    localStorage.setItem('app_site', JSON.stringify({ siteName: '旧站点', siteDescription: '旧描述' }))
    window.__LLMPOD_SETTINGS__ = FULL_SETTINGS

    runBoot()

    // 明暗为纯前端偏好:不种入 settings.mode,缓存不含 mode 键
    expect(JSON.parse(localStorage.getItem('app_remote_theme') ?? '{}')).toEqual({
      primary: '#1261a8',
      loginBackground: 'https://cdn.example.com/login.png',
      loginPrimary: '#0ea5e9',
    })
    expect(JSON.parse(localStorage.getItem('app_brand') ?? '{}')).toEqual({
      platformName: 'LLMPod',
      logo: '🚀',
      footerText: '© 2026 LLMPod',
      siteDescription: '企业级推理网关',
    })
    // 站点缓存清旧重写(种子为权威)
    expect(JSON.parse(localStorage.getItem('app_site') ?? '{}')).toEqual({
      siteName: 'AI 中台',
      siteDescription: '企业级推理网关',
    })
    // 主题色同步写入首屏 CSS 变量
    expect(document.documentElement.style.getPropertyValue('--c-primary')).toBe('#1261a8')
    // 网页标题 siteName(通用设置)优先:fixture 站点名「AI 中台」压过平台名
    expect(document.title).toBe('AI 中台')
    // SEO 描述跟随 siteDescription
    expect(document.querySelector('meta[name="description"]')?.getAttribute('content')).toBe(
      '企业级推理网关',
    )
    // siteName 未配置时回落 platformName,站点缓存同步去键(另以自定义平台名验证覆盖生效)
    window.__LLMPOD_SETTINGS__ = { ...FULL_SETTINGS, siteName: null, platformName: 'Acme 中台' }
    runBoot()
    expect(document.title).toBe('Acme 中台')
    expect(JSON.parse(localStorage.getItem('app_site') ?? '{}')).toEqual({
      siteDescription: '企业级推理网关',
    })
  })

  it('多标签竞态回归:seed 非空时每键单次终态写,不产生 newValue:null 瞬态', () => {
    // 复现条件:旧标签页已挂载 useStorage('app_remote_theme')(如管理端 useTheme),
    // 新标签页加载触发 boot。若 boot 先 removeItem 再 setItem,remove 派发的
    // {newValue:null} 会让旧标签页 useStorage 走 writeDefaults 把默认值 {} 写回,
    // 压掉紧随的 setItem —— app_remote_theme 变 {}、登录页背景丢失。
    // 不变量:每个键的首个 storage 事件即终态值(newValue 非 null),每键至多一次写。
    const events: Array<{ key: string | null; newValue: string | null }> = []
    const onStorage = (e: StorageEvent) => events.push({ key: e.key, newValue: e.newValue })
    window.addEventListener('storage', onStorage)
    localStorage.setItem(
      'app_remote_theme',
      JSON.stringify({ primary: '#111111', loginBackground: 'https://old.example.com/bg.png' }),
    )
    window.__LLMPOD_SETTINGS__ = FULL_SETTINGS

    try {
      runBoot()
    } finally {
      window.removeEventListener('storage', onStorage)
    }

    // 终态正确:过期键(loginBackground 旧值)被整体替换清掉
    expect(JSON.parse(localStorage.getItem('app_remote_theme') ?? '{}')).toEqual({
      primary: '#1261a8',
      loginBackground: 'https://cdn.example.com/login.png',
      loginPrimary: '#0ea5e9',
    })
    // 无 null 瞬态:本窗口监听到的事件(vueuse 同文档路径)newValue 一律非 null
    const themeEvents = events.filter((e) => e.key === 'app_remote_theme')
    for (const event of themeEvents) {
      expect(event.newValue).not.toBeNull()
    }
    // 单键单写:同键事件不超过一次
    const counts = new Map<string, number>()
    for (const event of events) counts.set(event.key ?? '', (counts.get(event.key ?? '') ?? 0) + 1)
    for (const [key, count] of counts) {
      if (key) expect(count, `键 ${key} 的写入次数`).toBeLessThanOrEqual(1)
    }
  })

  it('明暗只认本地偏好 app_theme:settings.mode 不参与(本地 dark 生效)', () => {
    // useColorMode 以裸字符串持久化本地偏好(非 JSON)
    localStorage.setItem('app_theme', 'dark')
    window.__LLMPOD_SETTINGS__ = FULL_SETTINGS

    runBoot()

    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(document.documentElement.style.colorScheme).toBe('dark')
  })

  it('对象但主题/品牌字段全 null(仅开维护开关):两键被清空不残留旧缓存', () => {
    localStorage.setItem('app_remote_theme', JSON.stringify({ primary: '#111111' }))
    localStorage.setItem('app_brand', JSON.stringify({ platformName: '旧品牌' }))
    window.__LLMPOD_SETTINGS__ = { ...FULL_SETTINGS, themeColor: null, mode: null, loginBackground: null, loginPrimary: null, platformName: null, logo: null, footerText: null, siteDescription: null, maintenanceMode: true }

    runBoot()

    expect(localStorage.getItem('app_remote_theme')).toBeNull()
    expect(localStorage.getItem('app_brand')).toBeNull()
    // 站点名字段仍有值 → app_site 正常写入(维护开关不影响站点缓存;描述已清空不写键)
    expect(JSON.parse(localStorage.getItem('app_site') ?? '{}')).toEqual({
      siteName: 'AI 中台',
    })
  })

  it('空字段不写键:mergeDefaults 语义下缺键才回落默认(null 会压掉默认值)', () => {
    document.title = '运营后台'
    window.__LLMPOD_SETTINGS__ = { ...FULL_SETTINGS, siteName: null, siteDescription: null, platformName: null, logo: null, footerText: null, loginPrimary: null }

    runBoot()

    // 品牌三字段全空 → 不写 app_brand 键(缺键回落默认,而非 null 压默认)
    expect(localStorage.getItem('app_brand')).toBeNull()
    // loginPrimary 空 → 主题缓存不含该键
    expect(JSON.parse(localStorage.getItem('app_remote_theme') ?? '{}')).not.toHaveProperty(
      'loginPrimary',
    )
    // siteName/platformName 均未配置 → 网页标题与 SEO 描述回落默认(不保留旧标题)
    expect(document.title).toBe('LLMPod')
    expect(document.querySelector('meta[name="description"]')?.getAttribute('content')).toBe(
      'LLMPod',
    )
  })

  describe('品牌接管:favicon 与首屏 splash logo', () => {
    /** 模拟三端 index.html 的静态 head/favicon 与 splash DOM(jsdom readyState=complete,boot 内同步应用 favicon) */
    function setupDom() {
      document.head.innerHTML = '<link rel="icon" type="image/x-icon" href="/logo.ico">'
      document.body.innerHTML =
        '<div id="app"><div class="app-splash"><div class="app-splash__logo"><svg></svg></div></div></div>'
    }

    afterEach(() => {
      document.head.innerHTML = ''
      document.body.innerHTML = ''
    })

    it('emoji logo:favicon 转 SVG data URI,splash 替换为文本', () => {
      setupDom()
      window.__LLMPOD_SETTINGS__ = FULL_SETTINGS

      runBoot()

      const link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')!
      expect(link.getAttribute('href')).toMatch(/^data:image\/svg\+xml,/)
      expect(decodeURIComponent(link.getAttribute('href')!)).toContain('🚀')
      expect(link.type).toBe('image/svg+xml')
      const splash = document.querySelector<HTMLElement>('.app-splash__logo')!
      expect(splash.textContent).toBe('🚀')
      expect(splash.querySelector('svg')).toBeNull()
    })

    it('图片 URL logo:favicon 直挂且移除 type,splash 替换为 img', () => {
      setupDom()
      window.__LLMPOD_SETTINGS__ = { ...FULL_SETTINGS, logo: 'https://cdn.example.com/logo.png' }

      runBoot()

      const link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')!
      expect(link.getAttribute('href')).toBe('https://cdn.example.com/logo.png')
      expect(link.getAttribute('type')).toBeNull()
      const img = document.querySelector<HTMLImageElement>('.app-splash__logo img')!
      expect(img.getAttribute('src')).toBe('https://cdn.example.com/logo.png')
    })

    it('未配置/已清空(settings null):favicon 回落 /logo.ico,splash 保持默认 SVG', () => {
      setupDom()
      window.__LLMPOD_SETTINGS__ = null

      runBoot()

      const link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')!
      expect(link.getAttribute('href')!.endsWith('/logo.ico')).toBe(true)
      expect(link.type).toBe('image/x-icon')
      expect(document.querySelector('.app-splash__logo svg')).not.toBeNull()
    })

    it('splash 晚于 boot 出现:MutationObserver 分支补替换', async () => {
      // boot 先跑(head 内联语义),splash 随 body 解析才出现
      document.head.innerHTML = '<link rel="icon" type="image/x-icon" href="/logo.ico">'
      window.__LLMPOD_SETTINGS__ = FULL_SETTINGS
      runBoot()
      expect(document.querySelector('.app-splash__logo')).toBeNull()

      document.body.innerHTML =
        '<div id="app"><div class="app-splash"><div class="app-splash__logo"><svg></svg></div></div></div>'
      // MutationObserver 回调在微任务派发
      await Promise.resolve()

      expect(document.querySelector<HTMLElement>('.app-splash__logo')!.textContent).toBe('🚀')
      // 结束后手动派发 DOMContentLoaded,断开 observer 避免跨用例残留
      document.dispatchEvent(new Event('DOMContentLoaded'))
    })
  })
})
