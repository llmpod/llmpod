// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { NDataTable } from 'naive-ui'
import { describe, expect, it } from 'vitest'
import { LmTable } from '../../src'

/** 挂载裸 LmTable,返回读取内层 n-data-table scrollbarProps 的函数 */
function mountTable(attrs: Record<string, unknown> = {}) {
  const wrapper = mount(LmTable, {
    props: { data: [], columns: [] },
    attrs,
  })
  const table = wrapper.findComponent(NDataTable)
  const scrollbarProps = () => table.props('scrollbarProps') as Record<string, unknown>
  return { wrapper, scrollbarProps }
}

describe('LmTable 横向滚动条常驻', () => {
  it('默认透传 scrollbarProps.trigger=none,横向溢出时滚动条不再滚动后淡出', () => {
    const { wrapper, scrollbarProps } = mountTable()
    expect(scrollbarProps().trigger).toBe('none')
    wrapper.unmount()
  })

  it('业务透传 scrollbar-props 时用户值优先,trigger 可覆盖、其余字段保留', () => {
    const { wrapper, scrollbarProps } = mountTable({
      'scrollbar-props': { trigger: 'hover', size: 8 },
    })
    expect(scrollbarProps().trigger).toBe('hover')
    expect(scrollbarProps().size).toBe(8)
    wrapper.unmount()
  })
})
