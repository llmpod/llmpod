import { describe, expect, it } from 'vitest'
import { splitReasoning } from '../../src'

describe('splitReasoning', () => {
  it('无标签:原文全部为可见正文', () => {
    expect(splitReasoning('你好,世界')).toEqual({ reasoning: '', visible: '你好,世界' })
  })

  it('空字符串:返回空结果', () => {
    expect(splitReasoning('')).toEqual({ reasoning: '', visible: '' })
  })

  it('闭合 think 块:拆出思考与正文', () => {
    const content = '<think>先分析问题</think>答案是 42'
    expect(splitReasoning(content)).toEqual({ reasoning: '先分析问题', visible: '答案是 42' })
  })

  it('未闭合 think 块:标签后全部视为思考(流式生成中)', () => {
    const content = '<think>用户想要'
    expect(splitReasoning(content)).toEqual({ reasoning: '用户想要', visible: '' })
  })

  it('think 块之前的正文保留在 visible', () => {
    const content = '前置说明<think>思考中'
    expect(splitReasoning(content)).toEqual({ reasoning: '思考中', visible: '前置说明' })
  })

  it('多个 think 块全部归入 reasoning,其余按顺序拼接为 visible', () => {
    const content = 'A<think>一</think>B<think>二</think>C'
    expect(splitReasoning(content)).toEqual({ reasoning: '一二', visible: 'ABC' })
  })

  it('标签内外首尾空白被去除', () => {
    const content = '<think>\n  思考  \n</think>\n\n正文\n'
    expect(splitReasoning(content)).toEqual({ reasoning: '思考', visible: '正文' })
  })

  it('幂等:流式场景反复调用同一累积 buffer 结果一致', () => {
    const buffer = '<think>正在推理'
    const first = splitReasoning(buffer)
    const second = splitReasoning(buffer)
    expect(first).toEqual(second)
  })
})
