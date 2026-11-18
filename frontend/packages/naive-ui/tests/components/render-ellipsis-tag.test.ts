// @vitest-environment happy-dom
import { defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { renderEllipsisTag, type EllipsisTagOptions } from '../../src'

/** 把渲染函数挂到一个宿主组件上,便于断言最终 DOM */
function mountTag(text: string, options: EllipsisTagOptions = {}) {
  const Host = defineComponent({
    name: 'EllipsisTagHost',
    setup: () => () => renderEllipsisTag(text, options),
  })
  return mount(Host)
}

describe('renderEllipsisTag 超长省略标签', () => {
  it('标签自身是块级文本容器,text-overflow 才能作用到 naive 的内容层', () => {
    const wrapper = mountTag('qwen2.5-72b-instruct-20250101')
    const tag = wrapper.find('.n-tag')
    expect(tag.exists()).toBe(true)

    const style = tag.attributes('style') ?? ''
    expect(style).toContain('display: block')
    expect(style).toContain('width: fit-content')
    expect(style).toContain('overflow: hidden')
    expect(style).toContain('text-overflow: ellipsis')
    expect(style).toContain('white-space: nowrap')
    // inline-flex 的垂直居中改由 line-height 接管,贴合 naive 自身高度变量
    expect(style).toContain('line-height: var(--n-height)')
    wrapper.unmount()
  })

  it('默认最大宽度 12rem,并用 min(…, 100%) 兜住更窄的列/卡片', () => {
    const wrapper = mountTag('m')
    expect(wrapper.find('.n-tag').attributes('style')).toContain('max-width: min(12rem, 100%)')
    wrapper.unmount()

    const narrow = mountTag('m', { maxWidth: 8 })
    expect(narrow.find('.n-tag').attributes('style')).toContain('max-width: min(8rem, 100%)')
    narrow.unmount()
  })

  it('不再使用原生 title 属性(悬浮提示交给 NTooltip)', () => {
    const wrapper = mountTag('gpt-4o-mini')
    expect(wrapper.find('.n-tag').attributes('title')).toBeUndefined()
    wrapper.unmount()
  })

  it('未溢出时不弹 tooltip:短文案不产生提示噪音', () => {
    const wrapper = mountTag('gpt-4o-mini')
    // happy-dom 无真实排版(scrollWidth === clientWidth),此时应判定为「未溢出」
    expect(document.querySelector('.n-tooltip')).toBeNull()
    wrapper.unmount()
  })

  it('透传配色/尺寸/描边到标签', () => {
    const wrapper = mountTag('engine', {
      type: 'success',
      size: 'medium',
      bordered: true,
      color: { color: '#eee', textColor: '#111' },
    })
    const style = wrapper.find('.n-tag').attributes('style') ?? ''
    expect(style).toContain('--n-color: #eee')
    expect(style).toContain('--n-text-color: #111')
    wrapper.unmount()
  })
})
