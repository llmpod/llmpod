import { describe, expect, it } from 'vitest'
import { escapeHtml } from '../src'

describe('escapeHtml', () => {
  it('转义标签尖括号', () => {
    expect(escapeHtml('<img src=x onerror=alert(1)>')).toBe('&lt;img src=x onerror=alert(1)&gt;')
    expect(escapeHtml('<script>alert(1)</script>')).toBe('&lt;script&gt;alert(1)&lt;/script&gt;')
  })

  it('转义属性上下文里的双引号与单引号', () => {
    expect(escapeHtml('" onmouseover="alert(1)')).toBe('&quot; onmouseover=&quot;alert(1)')
    expect(escapeHtml("' onmouseover='alert(1)")).toBe('&#39; onmouseover=&#39;alert(1)')
  })

  it('& 一并转义,已编码内容不会被二次解码还原', () => {
    expect(escapeHtml('&lt;script&gt;')).toBe('&amp;lt;script&amp;gt;')
    expect(escapeHtml('a & b')).toBe('a &amp; b')
  })

  it('null/undefined 返回空串,不把 "undefined" 拼进 tooltip', () => {
    expect(escapeHtml(null)).toBe('')
    expect(escapeHtml(undefined)).toBe('')
  })

  it('普通文本与数字原样返回', () => {
    expect(escapeHtml(42)).toBe('42')
    expect(escapeHtml('deployment-1 #3')).toBe('deployment-1 #3')
  })
})
