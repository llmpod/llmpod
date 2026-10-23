// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { LmTableCard, LmTableEmpty } from '../../src'

/* eslint-disable @typescript-eslint/no-explicit-any */
const columns: any[] = [
  { type: 'selection' },
  { title: '名称', key: 'name' },
  { title: '状态', key: 'status', render: (row: any) => h('span', { class: 'status-tag' }, row.status) },
  { title: '备注', key: 'remark' },
  { title: '地区', key: 'region' },
  { title: '创建时间', key: 'createdAt' },
  { title: '操作', key: 'operate', __lmOperate: true, render: (row: any) => h('div', { class: 'op' }, `操作-${row.name}`) },
]

const rows = [
  { id: 1, name: 'alpha', status: 'ok', remark: '', region: '华东', createdAt: '2026-02-14' },
  { id: 2, name: 'beta', status: 'fail', remark: 'r2', region: '华北', createdAt: '2026-02-13' },
]

function mountCard(props: Record<string, unknown> = {}) {
  return mount(LmTableCard, {
    props: {
      rows,
      columns,
      rowKey: (row: any) => row.id,
      ...props,
    },
  })
}

describe('LmTableCard 卡片列表', () => {
  it('每行渲染一张卡片,主标题取第一个数据列', () => {
    const wrapper = mountCard()
    const cards = wrapper.findAll('.lm-table-card__item')
    expect(cards).toHaveLength(2)
    expect(cards[0].find('.lm-table-card__subject').text()).toBe('alpha')
    wrapper.unmount()
  })

  it('定义列表 label-value 呈现:render 列复用其渲染函数,空值显示 —', () => {
    const wrapper = mountCard()
    const card = wrapper.findAll('.lm-table-card__item')[0]
    const rows0 = card.findAll('.lm-table-card__kvrow')
    // 名称升为主标题,其余候选列取前 3:状态 / 备注 / 地区
    expect(rows0).toHaveLength(3)
    expect(rows0[0].find('.lm-table-card__label').text()).toBe('状态')
    expect(rows0[0].find('.status-tag').exists()).toBe(true)
    // remark 空值显示占位符
    expect(rows0[1].find('.lm-table-card__value').text()).toBe('—')
    wrapper.unmount()
  })

  it('超出可见上限的字段收进「展开 N 项」,展开后才渲染明细', async () => {
    const wrapper = mountCard()
    const card = wrapper.findAll('.lm-table-card__item')[0]
    expect(card.find('.lm-table-card__hidden').exists()).toBe(false)

    const toggle = card.find('.lm-table-card__toggle')
    expect(toggle.text()).toContain('展开 1 项')
    await toggle.trigger('click')

    const hidden = card.find('.lm-table-card__hidden')
    expect(hidden.exists()).toBe(true)
    expect(hidden.find('.lm-table-card__label').text()).toBe('创建时间')
    expect(card.find('.lm-table-card__toggle').text()).toContain('收起明细')

    // 收起后明细回到未渲染状态
    await card.find('.lm-table-card__toggle').trigger('click')
    expect(card.find('.lm-table-card__hidden').exists()).toBe(false)
    wrapper.unmount()
  })

  it('无折叠字段时不渲染展开入口', () => {
    const wrapper = mountCard({ columns: columns.filter((c) => ['selection', 'name', 'status'].includes(c.key ?? c.type)) })
    expect(wrapper.find('.lm-table-card__toggle').exists()).toBe(false)
    wrapper.unmount()
  })

  it('操作列渲染在卡片页脚操作区', () => {
    const wrapper = mountCard()
    const extras = wrapper.findAll('.lm-table-card__extra')
    expect(extras).toHaveLength(2)
    expect(extras[0].text()).toBe('操作-alpha')
    wrapper.unmount()
  })

  it('emphasisKey 指定强调列,渲染在卡首强调位且不再进定义列表', () => {
    const wrapper = mountCard({ emphasisKey: 'createdAt' })
    const card = wrapper.findAll('.lm-table-card__item')[0]
    expect(card.find('.lm-table-card__amount').text()).toBe('2026-02-14')
    const labels = card.findAll('.lm-table-card__label').map((n) => n.text())
    expect(labels).not.toContain('创建时间')
    wrapper.unmount()
  })

  it('tagKey 指定标签列,渲染为卡首标签且不再进定义列表', () => {
    const wrapper = mountCard({ tagKey: 'status' })
    const card = wrapper.findAll('.lm-table-card__item')[0]
    expect(card.find('.lm-table-card__tag').find('.status-tag').exists()).toBe(true)
    const labels = card.findAll('.lm-table-card__label').map((n) => n.text())
    expect(labels).not.toContain('状态')
    wrapper.unmount()
  })

  it('alertWhen 命中时出边轨类,true 等价 error,未配置时无边轨', () => {
    const byTone = mountCard({
      alertWhen: (row: any) => (row.status === 'fail' ? 'error' : false),
    })
    const items = byTone.findAll('.lm-table-card__item')
    expect(items[0].classes()).not.toContain('is-alert-error')
    expect(items[1].classes()).toContain('is-alert-error')
    byTone.unmount()

    const byBool = mountCard({ alertWhen: (row: any) => row.status === 'fail' })
    expect(byBool.findAll('.lm-table-card__item')[1].classes()).toContain('is-alert-error')
    byBool.unmount()

    const none = mountCard()
    expect(none.findAll('.lm-table-card__item')[1].classes().join(' ')).not.toContain('is-alert')
    none.unmount()
  })

  it('selection 列存在时渲染 44px 勾选命中区,勾选发出 toggle-check 事件', async () => {
    const wrapper = mountCard({ checkedRowKeys: [2] })
    const picks = wrapper.findAll('.lm-table-card__pick')
    expect(picks).toHaveLength(2)
    // 第二张卡受控勾选:卡片带选中态类,勾选框 aria-checked 为真
    expect(wrapper.findAll('.lm-table-card__item')[1].classes()).toContain('is-sel')
    expect(picks[1].attributes('aria-checked')).toBe('true')

    await picks[0].trigger('click')
    const emitted = wrapper.emitted('toggleCheck')
    expect(emitted).toBeTruthy()
    expect(emitted![0]).toEqual([rows[0], true])
    wrapper.unmount()
  })

  it('取消勾选发出 toggle-check 载荷 [row, false]', async () => {
    const wrapper = mountCard({ checkedRowKeys: [1] })
    await wrapper.findAll('.lm-table-card__pick')[0].trigger('click')
    expect(wrapper.emitted('toggleCheck')![0]).toEqual([rows[0], false])
    wrapper.unmount()
  })

  it('无 selection 列时不渲染勾选命中区', () => {
    const wrapper = mountCard({ columns: columns.filter((c) => c.type !== 'selection') })
    expect(wrapper.find('.lm-table-card__pick').exists()).toBe(false)
    wrapper.unmount()
  })

  it('selection 列 disabled 禁用对应行勾选', async () => {
    const wrapper = mountCard({
      columns: [
        { type: 'selection', disabled: (row: any) => row.id === 1 },
        { title: '名称', key: 'name' },
      ],
    })
    const picks = wrapper.findAll('.lm-table-card__pick')
    expect(picks[0].attributes('disabled')).toBeDefined()
    expect(picks[1].attributes('disabled')).toBeUndefined()
    await picks[0].trigger('click')
    expect(wrapper.emitted('toggleCheck')).toBeUndefined()
    wrapper.unmount()
  })

  it('titleKey 指定主标题列,fieldKeys 决定可见字段', () => {
    const wrapper = mountCard({ titleKey: 'status', fieldKeys: ['name', 'region'] })
    expect(wrapper.find('.lm-table-card__subject').text()).toBe('ok')
    const labels = wrapper.findAll('.lm-table-card__item')[0]
      .findAll('.lm-table-card__label')
      .map((n) => n.text())
    expect(labels).toEqual(['名称', '地区'])
    wrapper.unmount()
  })

  it('density=compact 渲染紧凑行,不渲染勾选与行操作', () => {
    const wrapper = mountCard({ density: 'compact' })
    expect(wrapper.findAll('.lm-table-card__row')).toHaveLength(2)
    expect(wrapper.find('.lm-table-card__item').exists()).toBe(false)
    expect(wrapper.find('.lm-table-card__pick').exists()).toBe(false)
    expect(wrapper.find('.op').exists()).toBe(false)
    wrapper.unmount()
  })

  it('空数据渲染 #empty 插槽内容', () => {
    const wrapper = mount(LmTableCard, {
      props: { rows: [], columns, rowKey: (row: any) => row.id },
      slots: { empty: () => h(LmTableEmpty, { description: '还没有数据' }) },
    })
    expect(wrapper.text()).toContain('还没有数据')
    wrapper.unmount()
  })

  it('loading 态透传 n-spin 显示遮罩', async () => {
    const wrapper = mountCard({ loading: true })
    // n-spin 激活时 content 节点带 spinning 修饰类(实测 cssr 类名 n-spin-content--spinning)
    expect(wrapper.find('.n-spin-content').classes().join(' ')).toContain('n-spin-content--spinning')
    await wrapper.setProps({ loading: false })
    expect(wrapper.find('.n-spin-content').classes().join(' ')).not.toContain('n-spin-content--spinning')
    wrapper.unmount()
  })
})
