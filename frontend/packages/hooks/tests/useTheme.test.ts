// @vitest-environment jsdom

import { colord } from 'colord'
import { darkTheme } from 'naive-ui'
import { nextTick } from 'vue'
import { beforeAll, describe, expect, it } from 'vitest'

import { DEFAULT_THEME_CONFIG } from '../src/theme/constants'
import { addCssVarsToHtml, parseCssText } from '../src/theme/css-vars'
import { generatePalette, getPrimaryTextColor, getThemeColors } from '../src/theme/palette'
import { getThemeOverrides } from '../src/theme/overrides'
import { useTheme } from '../src/theme/useTheme'

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: () => ({
      matches: false,
      media: '(prefers-color-scheme: dark)',
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => true,
    }),
  })
  localStorage.clear()
})

describe('theme helpers', () => {
  it('generates complete palettes and Naive UI theme colors', () => {
    const palette = generatePalette(DEFAULT_THEME_CONFIG.primary, false)
    const colors = getThemeColors(DEFAULT_THEME_CONFIG, false)

    expect(palette).toHaveLength(10)
    expect(colors.primaryColor).toBe(palette[5])
    expect(colors.primaryColorHover).toBe(palette[4])
    expect(colors.primaryColorPressed).toBe(palette[6])
  })

  it('creates overrides and injects rgb css variables', () => {
    const overrides = getThemeOverrides(DEFAULT_THEME_CONFIG, false)
    addCssVarsToHtml(DEFAULT_THEME_CONFIG, false)

    expect(overrides.common?.primaryColor).toBeTruthy()
    expect(document.documentElement.style.getPropertyValue('--n-primary-color-6')).toMatch(
      /^\d+,\d+,\d+$/,
    )
    expect(parseCssText('--demo: 1,2,3; --next: value;')).toEqual({
      '--demo': '1,2,3',
      '--next': 'value',
    })
  })

  it('getThemeOverrides is pure and does not touch the DOM', () => {
    document.documentElement.style.cssText = ''
    getThemeOverrides(DEFAULT_THEME_CONFIG, true)
    expect(document.documentElement.style.cssText).toBe('')
  })

  it('caches palettes for repeated colors', () => {
    const first = generatePalette('#123456', true)
    const second = generatePalette('#123456', true)
    expect(second).toBe(first)
  })
})

describe('Menu overrides 活跃项取色', () => {
  /** 活跃态会被消费的两组键:文字走 --c-primary-text,图标/箭头走 --c-primary */
  const textKeys = ['itemTextColorActive', 'itemTextColorChildActive'] as const
  const iconKeys = ['itemIconColorActive', 'itemIconColorChildActive'] as const
  const arrowKeys = ['arrowColorActive', 'arrowColorChildActive'] as const

  for (const darkMode of [false, true]) {
    it(`${darkMode ? '暗色' : '亮色'}:文字与图标的基础态和 hover 变体同值`, () => {
      const menu = getThemeOverrides(DEFAULT_THEME_CONFIG, darkMode).Menu!

      for (const key of textKeys) {
        expect(menu[key]).toBe('var(--c-primary-text)')
        // naive 的悬浮样式走独立变量:漏给 hover 变体就会悬浮瞬间跳色(曾跳回正文白)
        expect(menu[`${key}Hover`]).toBe(menu[key])
      }
      for (const key of [...iconKeys, ...arrowKeys]) {
        expect(menu[key]).toBe('var(--c-primary)')
        expect(menu[`${key}Hover`]).toBe(menu[key])
      }
    })
  }

  it('活跃项文字不再退回正文色(--c-default)', () => {
    const menu = getThemeOverrides(DEFAULT_THEME_CONFIG, true).Menu!
    // 回归:暗色曾把 *Active / *ChildActive 及其 hover 全部设为 var(--c-default),
    // 导致选中项文字与主题色图标不同色、悬浮时又跳成白色
    const activeValues = [...textKeys, ...iconKeys, ...arrowKeys].map(
      (key) => menu[key] as string,
    )
    expect(activeValues).not.toContain('var(--c-default)')
  })
})

describe('getPrimaryTextColor(--c-primary-text 求解)', () => {
  it('亮色下主色已达标时原样返回', () => {
    expect(getPrimaryTextColor(DEFAULT_THEME_CONFIG.primary, false)).toBe(
      colord(DEFAULT_THEME_CONFIG.primary).toHex(),
    )
  })

  it('暗色下把主色沿 L 轴提亮到正文对比度,而不是固定某个色值', () => {
    const primary = '#7a1fa2'
    const text = getPrimaryTextColor(primary, true)

    // 回归:曾硬编码 #4088d3,导致换主题色后菜单选中项文字仍是蓝色
    expect(text).not.toBe('#4088d3')
    expect(text).not.toBe(colord(primary).toHex())
    // 提亮方向:HSL 的 L 必须高于主色本身
    expect(colord(text).toHsl().l).toBeGreaterThan(colord(primary).toHsl().l)
    // 色相保持(容差吸收 HSL 取整)
    expect(Math.abs(colord(text).toHsl().h - colord(primary).toHsl().h)).toBeLessThanOrEqual(2)
  })

  it('亮色下过浅的主色会被压深以达标', () => {
    const pale = '#4ade80'
    const text = getPrimaryTextColor(pale, false)
    expect(colord(text).toHsl().l).toBeLessThan(colord(pale).toHsl().l)
  })
})

/** 模拟另一同源标签页写入 app_theme:本标签页收到 storage 事件(useStorage 只更新 store) */
function dispatchCrossTabThemeChange(newValue: string, oldValue: string): void {
  localStorage.setItem('app_theme', newValue)
  window.dispatchEvent(
    new StorageEvent('storage', {
      key: 'app_theme',
      oldValue,
      newValue,
      storageArea: localStorage,
    }),
  )
}

describe('useTheme', () => {
  it('shares state and applies configured colors', () => {
    const first = useTheme()
    const second = useTheme()

    expect(first.themeConfig).toBe(second.themeConfig)
    first.setThemeConfig({ primary: '#ff4d4f' })
    expect(second.themeConfig.value.primary).toBe('#ff4d4f')
    expect(second.themeColors.value.primary).toHaveLength(10)
  })

  it('syncs css variables when the effective theme config changes', async () => {
    const theme = useTheme()
    theme.setThemeConfig({ primary: '#00ff00' })
    await nextTick()

    expect(document.documentElement.style.getPropertyValue('--c-primary')).toBe('#00ff00')
    expect(document.documentElement.style.getPropertyValue('--n-primary-color')).toMatch(
      /^\d+,\d+,\d+$/,
    )
  })

  it('--c-primary-text 随主题色变化联动(菜单选中项标题的取色来源)', async () => {
    const theme = useTheme()
    theme.setThemeMode('light')
    theme.setThemeConfig({ primary: '#7a1fa2' })
    await nextTick()
    const purple = document.documentElement.style.getPropertyValue('--c-primary-text')

    // 换主题色:同名变量必须跟着换,否则菜单活跃项会停在旧色(历史 bug:固定蓝)
    theme.setThemeConfig({ primary: '#0f766e' })
    await nextTick()
    const teal = document.documentElement.style.getPropertyValue('--c-primary-text')

    expect(teal).not.toBe(purple)
    // 色相跟随各自主题色(两个色在白底均已达 4.5:1,无需调整,故与主色同值)
    expect(purple).toBe('#7a1fa2')
    expect(teal).toBe('#0f766e')
  })

  it('亮色下过浅的主色压深成正文级变体后再注入', async () => {
    const theme = useTheme()
    theme.setThemeMode('light')
    theme.setThemeConfig({ primary: '#00ff00' })
    await nextTick()
    const text = document.documentElement.style.getPropertyValue('--c-primary-text')

    // 纯绿白底仅 1.37:1,必须压深;同时不能变成某个与主题色无关的固定值
    expect(text).not.toBe('#00ff00')
    expect(colord(text).toHsl().l).toBeLessThan(colord('#00ff00').toHsl().l)
    expect(colord(text).toHsl().h).toBeCloseTo(colord('#00ff00').toHsl().h, 0)
  })

  it('--c-primary-text 在暗色下取提亮变体,而非固定色', async () => {
    const theme = useTheme()
    theme.setThemeMode('dark')
    theme.setThemeConfig({ primary: '#7a1fa2' })
    await nextTick()
    const dark = document.documentElement.style.getPropertyValue('--c-primary-text')

    expect(dark).not.toBe('#4088d3')
    expect(colord(dark).toHsl().l).toBeGreaterThan(colord('#7a1fa2').toHsl().l)

    // 恢复到 light,保持后续用例对 mode 的既有前置假设
    theme.setThemeMode('light')
    await nextTick()
  })

  it('supports explicit modes and cycling toggle', async () => {
    const theme = useTheme()
    theme.setThemeMode('dark')
    await nextTick()
    expect(theme.darkMode.value).toBe(true)
    expect(theme.theme.value).toBe(darkTheme)
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    theme.toggleDarkMode()
    expect(['light', 'auto']).toContain(theme.mode.value)
  })

  it('keeps mode in sync when another tab rewrites app_theme (storage event)', async () => {
    const theme = useTheme()
    theme.setThemeMode('light')
    await nextTick()
    expect(theme.mode.value).toBe('light')
    expect(theme.darkMode.value).toBe(false)

    // 另一标签页切到深色:storage 事件只更新 colorMode.store,mode 必须跟随,
    // 否则 darkMode/theme/themeOverrides 全部停在旧值(bug: 暗色 token 叠亮色组件)
    dispatchCrossTabThemeChange('dark', 'light')
    await nextTick()

    expect(theme.mode.value).toBe('dark')
    expect(theme.darkMode.value).toBe(true)
    expect(theme.theme.value).toBe(darkTheme)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('recovers when user re-selects the stale mode after cross-tab change', async () => {
    const theme = useTheme()
    theme.setThemeMode('light')
    await nextTick()

    dispatchCrossTabThemeChange('dark', 'light')
    await nextTick()
    expect(theme.mode.value).toBe('dark')

    // 修复后 mode 已随 store 反向同步,再点「浅色」是跨值赋值,watch 正常触发
    theme.setThemeMode('light')
    await nextTick()

    expect(theme.mode.value).toBe('light')
    expect(theme.darkMode.value).toBe(false)
    expect(localStorage.getItem('app_theme')).toBe('light')
  })

  it('falls back to auto when another tab writes an invalid mode', async () => {
    const theme = useTheme()
    theme.setThemeMode('light')
    await nextTick()

    dispatchCrossTabThemeChange('neon', 'light')
    await nextTick()

    // 非法值经反向同步进 mode 后,由既有正向 watch 的 DEFAULT_THEME_MODE 兜底。
    // 注:storage 事件路径内 vueuse 暂停了 store→storage 回写,净化值不落盘,
    // 真实场景下刷新由初始化路径(useCycleList 读 store + 正向 watch 校正)自愈。
    expect(theme.mode.value).toBe('auto')
    expect(theme.darkMode.value).toBe(false)

    // 恢复 light,保持后续用例对 mode 的既有前置假设
    theme.setThemeMode('light')
    await nextTick()
  })
})

describe('initRemoteTheme', () => {
  it('applies remote primary/loginBackground, ignores remote mode (local preference)', async () => {
    const theme = useTheme()
    theme.initRemoteTheme({
      // mode 为纯前端本地偏好,后端下发不再消费
      mode: 'dark' as never,
      primary: '#ff0000',
      loginBackground: 'https://cdn.example.com/bg.png',
    })
    await nextTick()

    // 本地偏好保持 light,不被 remote mode 拉走
    expect(theme.mode.value).toBe('light')
    expect(theme.darkMode.value).toBe(false)
    expect(theme.loginBackground.value).toBe('https://cdn.example.com/bg.png')
    // 生效色板以后端 primary 为准,本地 themeConfig.primary 不再生效
    expect(theme.themeColors.value.primary).toEqual(generatePalette('#ff0000', false))
    // 缓存供首屏启动脚本(theme.boot.js)与下次冷启动使用;不含 mode 键
    const cached = JSON.parse(localStorage.getItem('app_remote_theme') ?? '{}')
    expect(cached).toMatchObject({
      primary: '#ff0000',
      loginBackground: 'https://cdn.example.com/bg.png',
    })
    expect(cached).not.toHaveProperty('mode')
  })

  it('ignores invalid fields and keeps the previous cache', () => {
    const theme = useTheme()
    theme.initRemoteTheme({ mode: 'neon' as never, primary: 'red', loginBackground: 42 as never })

    expect(theme.mode.value).toBe('light')
    expect(theme.loginBackground.value).toBe('https://cdn.example.com/bg.png')
    expect(theme.themeColors.value.primary).toEqual(generatePalette('#ff0000', false))
  })

  it('keeps local mode preference even when remote sends a valid mode', () => {
    const theme = useTheme()
    theme.initRemoteTheme({ primary: '#00ff00' })

    expect(theme.mode.value).toBe('light')
    expect(theme.themeColors.value.primary).toEqual(generatePalette('#00ff00', false))
  })

  it('applies and caches loginPrimary, falls back to primary when cleared/invalid', async () => {
    const theme = useTheme()
    theme.initRemoteTheme({ loginPrimary: '#0ea5e9' })
    await nextTick()

    expect(theme.loginPrimary.value).toBe('#0ea5e9')
    expect(JSON.parse(localStorage.getItem('app_remote_theme') ?? '{}').loginPrimary).toBe(
      '#0ea5e9',
    )

    // 非法 hex 被白名单丢弃,缓存保留原值
    theme.initRemoteTheme({ loginPrimary: 'red' as never })
    expect(theme.loginPrimary.value).toBe('#0ea5e9')

    // 清空后回落全站 primary(前序用例遗留 #00ff00)
    theme.initRemoteTheme({ loginPrimary: '' })
    expect(theme.loginPrimary.value).toBe('#00ff00')
  })
})
