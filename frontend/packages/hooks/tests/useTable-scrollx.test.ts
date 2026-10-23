// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it } from 'vitest'
import { LmTable as NaiveLmTable } from '@repo/naive-ui'
import { useTable } from '../src/components/table/useTable'
import { useCheckbox, useOperate } from '../src/components/table/table-plugin'
import { sumColumnWidth } from '../src/components/table/utils'

interface Row {
  id: number
  name: string
}

/** 挂载 useTable 并渲染短名 LmTable,返回读取内层表格 scrollX 的函数 */
async function mountTable(
  options: Parameters<typeof useTable>[0],
  tableAttrs?: Record<string, unknown>,
) {
  let api!: ReturnType<typeof useTable<Row>>
  const wrapper = mount(
    {
      setup() {
        api = useTable<Row>(options) as ReturnType<typeof useTable<Row>>
        return () => h('div', [h(api.LmTable, tableAttrs)])
      },
    },
    { attachTo: document.body },
  )
  await nextTick()
  const inner = wrapper.findComponent(NaiveLmTable)
  const scrollXOf = () => (inner.vm.$attrs as Record<string, unknown>)['scrollX']
  return { wrapper, scrollXOf }
}

const allFixedColumns = [
  useCheckbox<Row>(),
  { title: '名称', key: 'name', minWidth: 140 },
  { title: '状态', key: 'status', width: 100 },
  { title: '时间', key: 'created_at', width: 150 },
  useOperate<Row>([{ title: '详情', onClick: () => {} }], { width: 90 }),
] as Parameters<typeof useTable<Row>>['columns']

describe('sumColumnWidth 声明列宽求和', () => {
  it('全列声明 width/minWidth 时返回总和(selection 按默认 40 计)', () => {
    expect(sumColumnWidth(allFixedColumns as never)).toBe(40 + 140 + 100 + 150 + 90)
  })

  it('存在无声明宽的弹性列时返回 undefined(不注入 scrollX)', () => {
    expect(sumColumnWidth([{ title: '名称', key: 'name' }] as never)).toBeUndefined()
  })

  it('分组列(children)与百分比宽度返回 undefined', () => {
    const grouped = [
      { title: '组', key: 'g', children: [{ title: '子', key: 'c', width: 100 }] },
    ] as never
    expect(sumColumnWidth(grouped)).toBeUndefined()
    expect(sumColumnWidth([{ title: '比', key: 'p', width: '10%' }] as never)).toBeUndefined()
  })
})

describe('useTable 默认 scrollX 注入', () => {
  it('全列声明宽时内层表格拿到声明宽度总和', async () => {
    const { wrapper, scrollXOf } = await mountTable({
      request: async () => ({ data: [], total: 0 }),
      columns: allFixedColumns,
    })
    expect(scrollXOf()).toBe(40 + 140 + 100 + 150 + 90)
    wrapper.unmount()
  })

  it('存在弹性列时不注入(scrollX undefined,保持列压缩)', async () => {
    const { wrapper, scrollXOf } = await mountTable({
      request: async () => ({ data: [], total: 0 }),
      columns: [{ title: '名称', key: 'name' }, { title: '状态', key: 'status', width: 100 }],
    })
    expect(scrollXOf()).toBeUndefined()
    wrapper.unmount()
  })

  it('config.tableProps.scrollX 显式值优先于自动估算', async () => {
    const { wrapper, scrollXOf } = await mountTable({
      request: async () => ({ data: [], total: 0 }),
      columns: allFixedColumns,
      config: { tableProps: { scrollX: 1600 } },
    })
    expect(scrollXOf()).toBe(1600)
    wrapper.unmount()
  })

  it('模板 :scroll-x 透传值优先于自动估算', async () => {
    const { wrapper, scrollXOf } = await mountTable(
      {
        request: async () => ({ data: [], total: 0 }),
        columns: allFixedColumns,
      },
      { 'scroll-x': 1200 },
    )
    expect(scrollXOf()).toBe(1200)
    wrapper.unmount()
  })
})
