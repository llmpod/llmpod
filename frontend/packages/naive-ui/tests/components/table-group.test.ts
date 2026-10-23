// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { LmTableGroup } from '../../src'

describe('LmTableGroup 三段结构', () => {
  it('无 header/footer 插槽时不渲染头尾', () => {
    const wrapper = mount(LmTableGroup, {
      slots: { content: () => h('div', { class: 'body' }, '内容') },
    })
    expect(wrapper.find('header').exists()).toBe(false)
    expect(wrapper.find('footer').exists()).toBe(false)
    expect(wrapper.find('.body').text()).toBe('内容')
  })

  it('header 左右插槽渲染并分居两侧', () => {
    const wrapper = mount(LmTableGroup, {
      slots: {
        'header-left': () => h('span', { class: 'hl' }, '左'),
        'header-right': () => h('span', { class: 'hr' }, '右'),
        content: () => h('div'),
        'footer-left': () => h('span', { class: 'fl' }, '底左'),
        'footer-right': () => h('span', { class: 'fr' }, '底右'),
      },
    })
    expect(wrapper.find('header .hl').text()).toBe('左')
    expect(wrapper.find('header .hr').text()).toBe('右')
    expect(wrapper.find('footer .fl').text()).toBe('底左')
    expect(wrapper.find('footer .fr').text()).toBe('底右')
  })

  it('bordered/headerBordered 加边框类,gap 数字转 px 样式', () => {
    const wrapper = mount(LmTableGroup, {
      props: { bordered: true, headerBordered: true, gap: 12 },
      slots: {
        'header-left': () => h('span', 'h'),
        content: () => h('div'),
      },
    })
    expect(wrapper.find('header').classes()).toContain('border-b')
    expect(wrapper.find('section').attributes('style')).toContain('gap: 12px')
    // bordered 作用于内容容器
    expect(wrapper.html()).toContain('border-line')
  })

  it('gap 字符串原样使用', () => {
    const wrapper = mount(LmTableGroup, {
      props: { gap: '1.2rem' },
      slots: { content: () => h('div') },
    })
    expect(wrapper.find('section').attributes('style')).toContain('gap: 1.2rem')
  })

  it('header/footer 响应式锚点类与换行类存在(lm-table-group-header/footer + flex-wrap)', () => {
    const wrapper = mount(LmTableGroup, {
      slots: {
        'header-left': () => h('span', '左'),
        'header-right': () => h('span', '右'),
        content: () => h('div'),
        'footer-left': () => h('span', '底左'),
        'footer-right': () => h('span', '底右'),
      },
    })
    expect(wrapper.find('header.lm-table-group-header').exists()).toBe(true)
    expect(wrapper.find('header.lm-table-group-header').classes()).toContain('flex-wrap')
    expect(wrapper.find('footer.lm-table-group-footer').exists()).toBe(true)
    expect(wrapper.find('footer.lm-table-group-footer').classes()).toContain('flex-wrap')
    // 内嵌控件容器可收缩、可换行
    const row = wrapper.find('header.lm-table-group-header > div')
    expect(row.classes()).toContain('flex-wrap')
    expect(row.classes()).toContain('min-w-0')
    wrapper.unmount()
  })
})
