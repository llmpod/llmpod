import { describe, expect, it } from 'vitest'
import { NTag } from 'naive-ui'
import { renderEllipsisTag } from '../../src'

/** 取出渲染出的标签内联样式(helper 刻意全内联,避免依赖消费方 app 的 UnoCSS 扫描) */
function styleOf(vnode: ReturnType<typeof renderEllipsisTag>): Record<string, string> {
  return (vnode.props?.style ?? {}) as Record<string, string>
}

describe('renderEllipsisTag 超长省略标签', () => {
  it('产出 NTag,并把标签自身改为块级文本容器以便省略', () => {
    const vnode = renderEllipsisTag('qwen2.5-72b-instruct-20250101')
    expect(vnode.type).toBe(NTag)

    // 关键契约:inline-flex 下 naive 的 span.n-tag__content 不收缩,text-overflow 无从生效
    const style = styleOf(vnode)
    expect(style.display).toBe('block')
    expect(style.width).toBe('fit-content')
    expect(style.overflow).toBe('hidden')
    expect(style.textOverflow).toBe('ellipsis')
    expect(style.whiteSpace).toBe('nowrap')
    // inline-flex 的垂直居中改由 line-height 接管,贴合 naive 自身高度变量
    expect(style.lineHeight).toBe('var(--n-height)')
  })

  it('默认最大宽度 12rem,并用 min(…, 100%) 兜住更窄的列/卡片', () => {
    expect(styleOf(renderEllipsisTag('m')).maxWidth).toBe('min(12rem, 100%)')
    expect(styleOf(renderEllipsisTag('m', { maxWidth: 8 })).maxWidth).toBe('min(8rem, 100%)')
  })

  it('title 透传全名,配色默认 primary/small/无描边', () => {
    const vnode = renderEllipsisTag('gpt-4o-mini')
    expect(vnode.props?.title).toBe('gpt-4o-mini')
    expect(vnode.props?.type).toBe('primary')
    expect(vnode.props?.size).toBe('small')
    expect(vnode.props?.bordered).toBe(false)
  })

  it('支持自定义类型、配色与列表 key', () => {
    const vnode = renderEllipsisTag('engine', {
      type: 'success',
      size: 'medium',
      bordered: true,
      key: 'k1',
      color: { color: '#eee', textColor: '#111' },
    })
    expect(vnode.key).toBe('k1')
    expect(vnode.props?.type).toBe('success')
    expect(vnode.props?.size).toBe('medium')
    expect(vnode.props?.bordered).toBe(true)
    expect(vnode.props?.color).toEqual({ color: '#eee', textColor: '#111' })
  })
})
