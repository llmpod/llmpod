// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import { LmFooterText } from '@repo/naive-ui'
import { DEFAULT_BRAND_CONFIG } from '../src/brand/useBrand'
import type { LlmpodSettingsSeed } from '../src/settings'

// serverVersion 用例直接改写注入种子,每例后还原避免污染其它用例
afterEach(() => {
  window.__LLMPOD_SETTINGS__ = undefined
})

describe('LmFooterText', () => {
  it('默认文案原样显示,且恒显 github 图标链接(地址写死)', () => {
    const wrapper = mount(LmFooterText, { props: { text: DEFAULT_BRAND_CONFIG.footerText } })

    expect(wrapper.text()).toMatch(/© \d{4} LLMPod\. 保留所有权利。/)
    const link = wrapper.find('a')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('https://github.com/llmpod/llmpod')
    expect(link.attributes('target')).toBe('_blank')
    // ionicons LogoGithub 渲染为 svg
    expect(link.find('svg').exists()).toBe(true)
  })

  it('自定义文案不改变 github 图标:改文案后链接仍恒显', () => {
    const wrapper = mount(LmFooterText, { props: { text: '© 我的平台' } })

    expect(wrapper.text()).toContain('© 我的平台')
    expect(wrapper.find('a').attributes('href')).toBe('https://github.com/llmpod/llmpod')
  })

  it('文案含方括号等特殊字符按纯文本输出,不解析', () => {
    const wrapper = mount(LmFooterText, {
      props: { text: '文案 [icon:github](https://example.com) 结尾' },
    })

    // 仅一个 a 标签(固定 github 链接),文案中的标记样式文本原样输出
    expect(wrapper.findAll('a')).toHaveLength(1)
    expect(wrapper.text()).toContain('文案 [icon:github](https://example.com) 结尾')
  })

  it('注入 serverVersion 时 github 图标后展示版本号', () => {
    window.__LLMPOD_SETTINGS__ = { maintenanceMode: false, serverVersion: '26.9.3' } as LlmpodSettingsSeed
    const wrapper = mount(LmFooterText, { props: { text: '© 我的平台' } })

    expect(wrapper.text()).toContain('v26.9.3')
    // 版本号是紧跟 github 链接之后的纯文本兄弟节点
    expect(wrapper.find('a + span').exists()).toBe(true)
    expect(wrapper.find('a + span').text()).toBe('v26.9.3')
  })

  it('注入种子为 null(管理员未配置)时不展示版本号', () => {
    window.__LLMPOD_SETTINGS__ = null
    const wrapper = mount(LmFooterText, { props: { text: '© 我的平台' } })

    expect(wrapper.text()).not.toMatch(/v\d+/)
    expect(wrapper.find('a + span').exists()).toBe(false)
  })
})
