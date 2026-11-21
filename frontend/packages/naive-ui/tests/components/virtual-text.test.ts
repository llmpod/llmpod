// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { NVirtualList } from 'naive-ui'
import { defineComponent, h, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  LmVirtualText,
  estimateVirtualTextHeight,
  formatTextSize,
  prepareVirtualText,
  splitTextToVirtualLines,
  stringifyVirtualContent,
  type VirtualTextLine,
} from '../../src'

/** 捕获 NVirtualList(name='VirtualList') props 并渲染前若干项，规避 happy-dom 下 ResizeObserver 测得 0 高度问题 */
const VirtualListStub = defineComponent({
  name: 'VirtualList',
  props: {
    items: { type: Array, default: () => [] },
    itemSize: { type: Number, required: true },
    itemResizable: Boolean,
    keyField: { type: String, default: 'key' },
    paddingTop: { type: [Number, String], default: 0 },
    paddingBottom: { type: [Number, String], default: 0 },
  },
  setup(props, { slots }) {
    return () =>
      h(
        'div',
        { class: 'virtual-list-stub' },
        (props.items as VirtualTextLine[]).slice(0, 5).map((item, index) =>
          h('div', { key: item.key, class: 'virtual-row-stub' }, slots.default?.({ item, index })),
        ),
      )
  },
})

function stubClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('virtual-text 纯函数', () => {
  it('stringifyVirtualContent: 覆盖 null / JSON 字符串 / 普通字符串 / _raw / 对象与响应式 Proxy', () => {
    expect(stringifyVirtualContent(null)).toEqual({ text: '', empty: true, skippedPretty: false })
    expect(stringifyVirtualContent(undefined)).toEqual({ text: '', empty: true, skippedPretty: false })

    expect(stringifyVirtualContent('{"a":1,"b":2}')).toEqual({
      text: '{\n  "a": 1,\n  "b": 2\n}',
      empty: false,
      skippedPretty: false,
    })

    // 非 JSON 字符串直接原样返回
    expect(stringifyVirtualContent('data: {"delta":"hi"}\n\n')).toEqual({
      text: 'data: {"delta":"hi"}\n\n',
      empty: false,
      skippedPretty: false,
    })

    // 超出 prettyLimit 的大 JSON 字符串跳过 JSON.parse，避免主线程重解析阻塞
    const hugeJsonStr = `{"payload":"${'x'.repeat(200)}"}`
    expect(stringifyVirtualContent(hugeJsonStr, 100)).toEqual({
      text: hugeJsonStr,
      empty: false,
      skippedPretty: true,
    })

    // { _raw } 直接取原串
    expect(stringifyVirtualContent({ _raw: 'raw-sse-body' })).toEqual({
      text: 'raw-sse-body',
      empty: false,
      skippedPretty: false,
    })

    // 普通对象与 Vue reactive Proxy 均能正确格式化
    const proxyObj = reactive({ model: 'deepseek-v3', stream: true })
    expect(stringifyVirtualContent(proxyObj)).toEqual({
      text: '{\n  "model": "deepseek-v3",\n  "stream": true\n}',
      empty: false,
      skippedPretty: false,
    })
  })

  it('splitTextToVirtualLines: 兼容 CRLF、保留空行并物理切分超长单行', () => {
    const longLine = 'A'.repeat(50)
    const input = `line1\r\n\r\n${longLine}\nlast`
    const { lines, logicalLineCount, truncated } = splitTextToVirtualLines(input, {
      maxLineChars: 20,
    })

    expect(truncated).toBe(false)
    expect(logicalLineCount).toBe(4)
    expect(lines.map((row) => ({ lineNo: row.lineNo, wrapped: row.wrapped, text: row.text }))).toEqual([
      { lineNo: 1, wrapped: false, text: 'line1' },
      { lineNo: 2, wrapped: false, text: '' },
      { lineNo: 3, wrapped: false, text: 'A'.repeat(20) },
      { lineNo: null, wrapped: true, text: 'A'.repeat(20) },
      { lineNo: null, wrapped: true, text: 'A'.repeat(10) },
      { lineNo: 4, wrapped: false, text: 'last' },
    ])
  })

  it('splitTextToVirtualLines: 切分边界不拆碎 UTF-16 代理对（emoji）', () => {
    // 15 个 'a' + 1 个占 2 code units 的 emoji '🚀' + 'tail'；maxLineChars=16 恰好落在代理对中间
    const text = `${'a'.repeat(15)}🚀tail`
    const { lines } = splitTextToVirtualLines(text, { maxLineChars: 16 })

    expect(lines).toHaveLength(2)
    expect(lines[0].text).toBe('a'.repeat(15))
    expect(lines[1].text).toBe('🚀tail')
    expect(lines.map((row) => row.text).join('')).toBe(text)
  })

  it('prepareVirtualText: 触及 maxVirtualLines 上限时截断视图行但保留完整 text 供复制', () => {
    const raw = Array.from({ length: 10 }, (_, idx) => `row-${idx}`).join('\n')
    const prepared = prepareVirtualText(raw, { maxVirtualLines: 4 })

    expect(prepared.empty).toBe(false)
    expect(prepared.truncated).toBe(true)
    // 4 行内容 + 1 行截断提示
    expect(prepared.lines).toHaveLength(5)
    expect(prepared.lines[4].wrapped).toBe(true)
    expect(prepared.lines[4].text).toContain('复制')
    // 全文保持完整
    expect(prepared.text).toBe(raw)
  })

  it('estimateVirtualTextHeight: 短内容紧凑，长内容封顶 maxHeight', () => {
    const shortLines = splitTextToVirtualLines('{\n  "ok": true\n}').lines
    expect(estimateVirtualTextHeight(shortLines, { maxHeight: 240 })).toBe(76)

    const manyLines = splitTextToVirtualLines(
      Array.from({ length: 200 }, (_, idx) => `line ${idx}`).join('\n'),
    ).lines
    expect(estimateVirtualTextHeight(manyLines, { maxHeight: 240 })).toBe(240)
  })

  it('formatTextSize: 字符 / KB / MB 格式化', () => {
    expect(formatTextSize(256)).toBe('256 字符')
    expect(formatTextSize(2048)).toBe('2.0 KB')
    expect(formatTextSize(150 * 1024)).toBe('150 KB')
    expect(formatTextSize(2.5 * 1024 * 1024)).toBe('2.50 MB')
  })
})

describe('LmVirtualText 组件', () => {
  it('空值时渲染占位符，不挂载 NVirtualList 且不展示复制按钮', () => {
    const wrapper = mount(LmVirtualText, {
      props: { title: '请求体', content: null },
      global: { stubs: { VirtualList: VirtualListStub } },
    })

    expect(wrapper.text()).toContain('请求体')
    expect(wrapper.text()).toContain('—')
    expect(wrapper.findComponent(VirtualListStub).exists()).toBe(false)
    expect(wrapper.find('button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('大对象默认 defer=true 时先展示解析占位，下一帧挂载 NVirtualList 且传数字 padding/itemSize', async () => {
    let rafCallback: FrameRequestCallback | null = null
    const cancelRaf = vi.fn()
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      rafCallback = cb
      return 42
    })
    vi.stubGlobal('cancelAnimationFrame', cancelRaf)

    const payload = {
      model: 'gpt-4o',
      data: 'Z'.repeat(600),
    }

    const wrapper = mount(LmVirtualText, {
      props: { title: '请求体', content: payload },
      global: { stubs: { VirtualList: VirtualListStub } },
    })

    // 首帧未阻塞，先展示轻量解析占位
    expect(wrapper.text()).toContain('内容解析中')
    expect(wrapper.findComponent(VirtualListStub).exists()).toBe(false)

    // 触发下一帧回调
    expect(rafCallback).not.toBeNull()
    rafCallback!(16)
    await nextTick()

    const listComp = wrapper.findComponent(VirtualListStub)
    expect(listComp.exists()).toBe(true)
    expect(listComp.props('itemSize')).toBe(20)
    expect(listComp.props('itemResizable')).toBe(true)
    expect(typeof listComp.props('paddingTop')).toBe('number')
    expect(typeof listComp.props('paddingBottom')).toBe('number')

    // 600 字符被按 240 切分后，总虚拟行数大于原始 4 行，且每行均不超过 240 字符
    const items = listComp.props('items') as VirtualTextLine[]
    expect(items.length).toBeGreaterThan(4)
    expect(items.every((row) => row.text.length <= 240)).toBe(true)

    wrapper.unmount()
  })

  it('未到解析帧即卸载时取消挂起的 requestAnimationFrame', () => {
    const cancelRaf = vi.fn()
    vi.stubGlobal('requestAnimationFrame', () => 99)
    vi.stubGlobal('cancelAnimationFrame', cancelRaf)

    const wrapper = mount(LmVirtualText, {
      props: { content: { huge: 'X'.repeat(500) } },
      global: { stubs: { VirtualList: VirtualListStub } },
    })
    wrapper.unmount()

    expect(cancelRaf).toHaveBeenCalledWith(99)
  })

  it('点击复制按钮复制完整格式化原文（非切片或截断视图）', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard(writeText)

    const payload = { token: 'A'.repeat(500) }
    const wrapper = mount(LmVirtualText, {
      props: {
        title: '响应体',
        content: payload,
        defer: false,
        maxLineChars: 40,
        maxVirtualLines: 3,
      },
      global: { stubs: { VirtualList: VirtualListStub } },
    })

    const copyBtn = wrapper.find('button')
    expect(copyBtn.exists()).toBe(true)
    await copyBtn.trigger('click')
    await nextTick()

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(JSON.stringify(payload, null, 2))
    expect(wrapper.text()).toContain('已复制')

    wrapper.unmount()
  })

  it('保留 NVirtualList 真实组件引用检查（确保导出类型完整）', () => {
    expect(NVirtualList).toBeDefined()
  })
})
