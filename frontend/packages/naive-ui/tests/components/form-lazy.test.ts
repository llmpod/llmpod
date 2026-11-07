// @vitest-environment happy-dom
import { h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { LmDatePicker, LmDatePickerAsync, LmTreeSelect, LmTreeSelectAsync } from '../../src'
import { mountWithProvider } from '../helpers/mount'

interface AsyncComponentMeta {
  __asyncLoader?: unknown
  __asyncResolved?: unknown
}

describe('低频重字段的异步入口', () => {
  it('导出的是异步组件入口，且与同步导出相互独立', () => {
    for (const comp of [LmDatePickerAsync, LmTreeSelectAsync]) {
      expect((comp as AsyncComponentMeta).__asyncLoader).toBeTypeOf('function')
    }
    // 同步导出保持原样，供直接引用与既有测试使用
    expect(LmDatePicker).toBeTypeOf('object')
    expect(LmTreeSelect).toBeTypeOf('object')
    expect(LmDatePickerAsync).not.toBe(LmDatePicker)
    expect(LmTreeSelectAsync).not.toBe(LmTreeSelect)
  })

  it('异步入口加载完成后渲染出与同步版一致的控件，并缓存已解析的实现模块', async () => {
    const picker = mountWithProvider(() => h(LmDatePickerAsync, { placeholder: '选择日期' }))
    await flushPromises()
    expect(picker.find('input').exists()).toBe(true)
    // 断言"已解析"而非"未解析"：无需依赖用例执行顺序
    expect((LmDatePickerAsync as AsyncComponentMeta).__asyncResolved).toBeTruthy()

    const tree = mountWithProvider(() =>
      h(LmTreeSelectAsync, { placeholder: '选择节点', options: [{ key: 'n1', label: '节点1' }] }),
    )
    await flushPromises()
    expect(tree.find('.n-base-selection').exists()).toBe(true)
    expect((LmTreeSelectAsync as AsyncComponentMeta).__asyncResolved).toBeTruthy()
  })
})
