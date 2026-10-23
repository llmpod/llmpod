// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { LmTableCard } from '@repo/naive-ui'
import { useTable } from '../src/components/table/useTable'

/* eslint-disable @typescript-eslint/no-explicit-any */
type MatchListener = (event: MediaQueryListEvent) => void

/** 模块级断点控制器:mockMatchMedia 只在 beforeAll 安装一次,change() 复用同一监听集合 */
let mediaCtl: { change: (next: boolean) => void }

function mockMatchMedia(initialMatches: boolean) {
  let matches = initialMatches
  const listeners = new Set<MatchListener>()
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      matches: query === '(max-width: 767px)' && matches,
      media: query,
      onchange: null,
      addEventListener: (_t: string, fn: MatchListener) => listeners.add(fn),
      removeEventListener: (_t: string, fn: MatchListener) => listeners.delete(fn),
      addListener: (fn: MatchListener) => listeners.add(fn),
      removeListener: (fn: MatchListener) => listeners.delete(fn),
      dispatchEvent: () => true,
    }),
  })
  return {
    change(next: boolean) {
      matches = next
      listeners.forEach((fn) => fn({ matches: next } as MediaQueryListEvent))
    },
  }
}

interface Row {
  id: number
  name: string
}

async function mountTable(options: any) {
  let api!: any
  const wrapper = mount(
    {
      setup() {
        api = useTable<Row>(options)
        // 统一挂 empty 插槽:卡片空态透传断言依赖它
        return () => h('div', [h(api.LmTable, undefined, { empty: () => h('span', '空态引导') })])
      },
    },
    { attachTo: document.body },
  )
  await nextTick()
  return { wrapper, api }
}

describe('useTable LmTable 窄屏卡片模式', () => {
  beforeAll(() => {
    mediaCtl = mockMatchMedia(true)
  })

  // 每例后复位为移动端断点,避免单例 matchMedia mock 状态泄漏到后续用例
  afterEach(() => mediaCtl.change(true))

  it('移动端断点(≤767)渲染卡片而非 n-data-table', async () => {
    const { wrapper } = await mountTable({
      request: async () => ({ data: [{ id: 1, name: '行一' }], total: 1 }),
      columns: [{ title: '名称', key: 'name' }],
    })
    expect(wrapper.findComponent(LmTableCard).exists()).toBe(true)
    expect(wrapper.find('.n-data-table').exists()).toBe(false)
    wrapper.unmount()
  })

  it('断点跨回桌面后恢复表格形态', async () => {
    const { wrapper } = await mountTable({
      request: async () => ({ data: [], total: 0 }),
      columns: [{ title: '名称', key: 'name' }],
    })
    expect(wrapper.findComponent(LmTableCard).exists()).toBe(true)
    mediaCtl.change(false)
    await nextTick()
    expect(wrapper.findComponent(LmTableCard).exists()).toBe(false)
    expect(wrapper.find('.n-data-table').exists()).toBe(true)
    wrapper.unmount()
  })

  it('config.card.enabled=false 时移动端保持表格', async () => {
    const { wrapper } = await mountTable({
      request: async () => ({ data: [], total: 0 }),
      columns: [{ title: '名称', key: 'name' }],
      config: { card: { enabled: false } },
    })
    expect(wrapper.findComponent(LmTableCard).exists()).toBe(false)
    expect(wrapper.find('.n-data-table').exists()).toBe(true)
    wrapper.unmount()
  })

  it('卡片勾选与 selectedKeys/selectedRows 同步', async () => {
    const { wrapper, api } = await mountTable({
      request: async () => ({
        data: [
          { id: 1, name: 'a' },
          { id: 2, name: 'b' },
        ],
        total: 2,
      }),
      columns: [{ type: 'selection' }, { title: '名称', key: 'name' }],
    })
    await vi.waitFor(() => expect(api.data.value.length).toBe(2))
    const card = wrapper.findComponent(LmTableCard)
    card.vm.$emit('toggleCheck', { id: 1, name: 'a' }, true)
    await nextTick()
    expect(api.selectedKeys.value).toEqual([1])
    expect(api.selectedRows.value).toEqual([{ id: 1, name: 'a' }])
    card.vm.$emit('toggleCheck', { id: 1, name: 'a' }, false)
    await nextTick()
    expect(api.selectedKeys.value).toEqual([])
    wrapper.unmount()
  })

  it('空数据时透传 #empty 插槽给卡片', async () => {
    const { wrapper } = await mountTable({
      request: async () => ({ data: [], total: 0 }),
      columns: [{ title: '名称', key: 'name' }],
    })
    await vi.waitFor(() => expect(wrapper.text()).toContain('空态引导'))
    wrapper.unmount()
  })

  it('titleKey/fieldKeys 透传给卡片(config.card)', async () => {
    const { wrapper } = await mountTable({
      request: async () => ({ data: [{ id: 1, name: 'a', status: 'ok' }], total: 1 }),
      columns: [
        { title: '名称', key: 'name' },
        { title: '状态', key: 'status' },
      ],
      config: { card: { titleKey: 'status', fieldKeys: ['name'] } },
    })
    const card = wrapper.findComponent(LmTableCard)
    expect(card.props('titleKey')).toBe('status')
    expect(card.props('fieldKeys')).toEqual(['name'])
    wrapper.unmount()
  })

  it('emphasisKey/tagKey/alertWhen/density 透传给卡片(config.card)', async () => {
    const alertWhen = () => true
    const { wrapper } = await mountTable({
      request: async () => ({ data: [{ id: 1, name: 'a', amount: 1, status: 'ok' }], total: 1 }),
      columns: [
        { title: '名称', key: 'name' },
        { title: '金额', key: 'amount' },
        { title: '状态', key: 'status' },
      ],
      config: {
        card: {
          titleKey: 'name',
          emphasisKey: 'amount',
          tagKey: 'status',
          alertWhen,
          density: 'compact',
        },
      },
    })
    const card = wrapper.findComponent(LmTableCard)
    expect(card.props('emphasisKey')).toBe('amount')
    expect(card.props('tagKey')).toBe('status')
    expect(card.props('alertWhen')).toBe(alertWhen)
    expect(card.props('density')).toBe('compact')
    wrapper.unmount()
  })

  it('未配置新卡面字段时走缺省(强调/标签空串,密度 card)', async () => {
    const { wrapper } = await mountTable({
      request: async () => ({ data: [{ id: 1, name: 'a' }], total: 1 }),
      columns: [{ title: '名称', key: 'name' }],
    })
    const card = wrapper.findComponent(LmTableCard)
    expect(card.props('emphasisKey')).toBe('')
    expect(card.props('tagKey')).toBe('')
    expect(card.props('density')).toBe('card')
    wrapper.unmount()
  })
})
