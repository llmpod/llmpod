// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import { LmThemeModeCard } from '@repo/naive-ui'
import { useTheme } from '../src/theme/useTheme'

beforeEach(() => {
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

describe('LmThemeModeCard', () => {
  it('渲染 自动/浅色/深色 三张卡片与说明文字', () => {
    const wrapper = mount(LmThemeModeCard)
    const labels = wrapper.findAll('button').map((btn) => btn.text())
    expect(labels).toEqual(['自动', '浅色', '深色'])
    expect(wrapper.text()).toContain('选择界面主题风格，影响整体界面的明暗风格')
  })

  it('点击深色卡片:切换明暗为本地偏好 dark 并高亮选中态', async () => {
    const wrapper = mount(LmThemeModeCard)
    const theme = useTheme()

    const darkBtn = wrapper.findAll('button').find((btn) => btn.text() === '深色')!
    await darkBtn.trigger('click')

    expect(theme.mode.value).toBe('dark')
    expect(theme.darkMode.value).toBe(true)
    expect(darkBtn.attributes('aria-pressed')).toBe('true')
    // 本地偏好持久化到 app_theme(useColorMode 以裸字符串持久化,非 JSON)
    expect(localStorage.getItem('app_theme')).toBe('dark')
  })

  it('切回自动:跟随系统偏好,不强制明暗', async () => {
    const wrapper = mount(LmThemeModeCard)
    const theme = useTheme()

    const darkBtn = wrapper.findAll('button').find((btn) => btn.text() === '深色')!
    await darkBtn.trigger('click')
    const autoBtn = wrapper.findAll('button').find((btn) => btn.text() === '自动')!
    await autoBtn.trigger('click')

    expect(theme.mode.value).toBe('auto')
    // matchMedia stub 为 light:auto 解析为亮色
    expect(theme.darkMode.value).toBe(false)
  })
})
