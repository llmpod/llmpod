// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { LmTablePage } from '../../src'

describe('LmTablePage 总数展示', () => {
  it('桌面分页器前缀显示「共 N 条」', () => {
    const wrapper = mount(LmTablePage, { props: { total: 42 } })
    expect(wrapper.find('.n-pagination-prefix').text()).toBe('共 42 条')
    wrapper.unmount()
  })

  it('总数为 0 时同样显示「共 0 条」', () => {
    const wrapper = mount(LmTablePage, { props: { total: 0 } })
    expect(wrapper.find('.n-pagination-prefix').text()).toBe('共 0 条')
    wrapper.unmount()
  })

  it('页面显式传 prefix 插槽时覆盖内置总数', () => {
    const wrapper = mount(LmTablePage, {
      props: { total: 7 },
      slots: { prefix: () => h('span', { class: 'custom-prefix' }, '自定义前缀') },
    })
    expect(wrapper.find('.n-pagination-prefix').text()).toBe('自定义前缀')
    expect(wrapper.find('.lm-table-page__total').exists()).toBe(false)
    wrapper.unmount()
  })
})
