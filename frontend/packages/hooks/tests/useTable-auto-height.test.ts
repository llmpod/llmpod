// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { LmTable as NaiveLmTable } from '@repo/naive-ui'
import { useTable } from '../src/components/table/useTable'

interface Row {
  id: number
  name: string
}

/** 挂载 useTable 并渲染其短名 LmTable,返回读取内层 NaiveLmTable 透传 maxHeight 的函数 */
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
  const attrOf = () => (inner.vm.$attrs as Record<string, unknown>)['maxHeight']
  return { wrapper, attrOf }
}

describe('useTable LmTable 视口自适应 max-height', () => {
  it('未显式传 maxHeight 时,内层表格拿到 calc(100vh - Npx) 形式的自适应高度', async () => {
    const { wrapper, attrOf } = await mountTable({
      request: async () => ({ data: [], total: 0 }),
      columns: [],
    })
    // happy-dom 无真实布局,只断言形态:calc 表达式已生成并透传
    expect(attrOf()).toMatch(/^calc\(100vh - \d+px\)$/)
    wrapper.unmount()
  })

  it('显式传 maxHeight 时原样透传,不启用自适应(向导/弹窗定高场景)', async () => {
    const { wrapper, attrOf } = await mountTable(
      { request: async () => ({ data: [], total: 0 }), columns: [] },
      { maxHeight: 320 },
    )
    expect(attrOf()).toBe(320)
    wrapper.unmount()
  })

  it('maxHeight 为 0 是合法显式值,同样原样透传', async () => {
    const { wrapper, attrOf } = await mountTable(
      { request: async () => ({ data: [], total: 0 }), columns: [] },
      { maxHeight: 0 },
    )
    expect(attrOf()).toBe(0)
    wrapper.unmount()
  })

  it('config.tableProps.maxHeight 与模板显式值并存时,模板优先', async () => {
    const { wrapper, attrOf } = await mountTable(
      {
        request: async () => ({ data: [], total: 0 }),
        columns: [],
        config: { tableProps: { maxHeight: 555 } },
      },
      { maxHeight: 333 },
    )
    expect(attrOf()).toBe(333)
    wrapper.unmount()
  })

  it('仅 config.tableProps.maxHeight 时使用它,不启用自适应', async () => {
    const { wrapper, attrOf } = await mountTable({
      request: async () => ({ data: [], total: 0 }),
      columns: [],
      config: { tableProps: { maxHeight: 555 } },
    })
    expect(attrOf()).toBe(555)
    wrapper.unmount()
  })
})
