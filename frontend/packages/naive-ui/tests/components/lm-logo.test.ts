// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import { useBrand } from '@repo/hooks'
import LmLogo from '../../src/components/layout/LmLogo.vue'

describe('LmLogo 品牌接管', () => {
  const { brandConfig, setBrandConfig } = useBrand()

  afterEach(() => setBrandConfig({ logo: '' }))

  it('未配置 logo:渲染默认 SVG', () => {
    expect(brandConfig.value.logo).toBe('')
    const wrapper = mount(LmLogo, { props: { size: 22 } })
    expect(wrapper.find('svg').exists()).toBe(true)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('span').exists()).toBe(false)
  })

  it('emoji logo:渲染文本节点,字号随 size(px)', async () => {
    setBrandConfig({ logo: '🚀' })
    await nextTick()

    const wrapper = mount(LmLogo, { props: { size: 24 } })
    const span = wrapper.find('span')
    expect(span.text()).toBe('🚀')
    expect(span.attributes('style')).toContain('font-size: 24px')
    expect(wrapper.find('svg').exists()).toBe(false)
  })

  it('图片 logo:渲染 img,外部 class 透传合并', async () => {
    setBrandConfig({ logo: 'https://cdn.example.com/logo.png' })
    await nextTick()

    const wrapper = mount(LmLogo, { props: { size: 22 }, attrs: { class: 'text-primary' } })
    const img = wrapper.find('img')
    expect(img.attributes('src')).toBe('https://cdn.example.com/logo.png')
    expect(img.attributes('class')).toContain('text-primary')
    expect(img.attributes('class')).toContain('object-contain')
  })

  it('logo 带空白:trim 后判定与渲染', async () => {
    setBrandConfig({ logo: '  🚀  ' })
    await nextTick()

    const wrapper = mount(LmLogo, { props: { size: 22 } })
    expect(wrapper.find('span').text()).toBe('🚀')
  })
})
