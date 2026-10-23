// jsdom:dompurify 的节点遍历删除在 happy-dom 下会错位(误删白名单标签/漏删 script),消毒断言必须在 jsdom 跑
// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { sanitizeNotesHtml } from '../src/views/about/notes-html'

describe('sanitizeNotesHtml 更新日志消毒', () => {
  it('空输入返回空串', () => {
    expect(sanitizeNotesHtml('')).toBe('')
  })

  it('纯文本:HTML 转义 + 单换行转 <br>(观感对齐 whitespace-pre-line)', () => {
    const html = sanitizeNotesHtml('支持 a<b 与 & 符号\n第二行')
    expect(html).toContain('a&lt;b')
    expect(html).toContain('&amp;')
    expect(html).toContain('<br>')
  })

  it('HTML 片段:排版标签与外链保留', () => {
    const html = sanitizeNotesHtml(
      '<ul><li><strong>新功能</strong></li></ul><a href="https://example.com/changelog">详情</a>',
    )
    expect(html).toContain('<strong>新功能</strong>')
    expect(html).toContain('href="https://example.com/changelog"')
  })

  it('脚本与事件属性剥离', () => {
    const html = sanitizeNotesHtml('<p onclick="alert(1)">x</p><script>alert(2)</script>')
    expect(html).not.toContain('script')
    expect(html).not.toContain('onclick')
  })

  it('style 颜色声明保留,非白名单属性与引用型值剥离', () => {
    const html = sanitizeNotesHtml(
      '<span style="color:#ff0000;background-color:rgba(0,0,0,.1);position:fixed;background:url(//e.com/x)">新</span>',
    )
    expect(html).toContain('color: #ff0000')
    expect(html).toContain('background-color: rgba(0,0,0,.1)')
    expect(html).not.toContain('position')
    expect(html).not.toContain('url')
  })

  it('font color 老式颜色写法保留', () => {
    const html = sanitizeNotesHtml('<font color="red">重点</font>')
    expect(html).toContain('color="red"')
  })

  it('style 声明全部不合规:整体移除 style 属性', () => {
    const html = sanitizeNotesHtml('<span style="position:fixed;top:0">x</span>')
    expect(html).not.toContain('style')
  })

  it('javascript: 协议链接剥离', () => {
    const html = sanitizeNotesHtml('<a href="javascript:alert(1)">x</a>')
    expect(html).not.toContain('javascript:')
  })
})
