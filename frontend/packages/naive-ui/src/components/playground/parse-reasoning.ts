/** <think> 标签解析:把混在 content 里的思考过程拆出来。 */

export interface SplitReasoningResult {
  /** 标签内的思考内容(多块拼接,首尾空白去除)。 */
  reasoning: string
  /** 标签外的可见正文(按原顺序拼接,首尾空白去除)。 */
  visible: string
}

const THINK_OPEN = '<think>'
const THINK_CLOSE = '</think>'

/**
 * 从流式/完整 content 中拆出 <think>...</think> 思考内容与可见正文。
 *
 * - 无标签:visible 为原文
 * - 未闭合的 <think>:其后全部内容视为思考流式生成中
 * - 支持多个 think 块:标签内全部归 reasoning,其余按顺序归 visible
 *
 * 纯函数且幂等,流式场景可对累积 buffer 反复调用。
 */
export function splitReasoning(content: string): SplitReasoningResult {
  if (!content.includes(THINK_OPEN)) {
    return { reasoning: '', visible: content }
  }

  let reasoning = ''
  let visible = ''
  let rest = content

  while (true) {
    const openIndex = rest.indexOf(THINK_OPEN)
    if (openIndex === -1) {
      visible += rest
      break
    }
    visible += rest.slice(0, openIndex)
    rest = rest.slice(openIndex + THINK_OPEN.length)

    const closeIndex = rest.indexOf(THINK_CLOSE)
    if (closeIndex === -1) {
      // 未闭合:思考仍在生成,后续内容全部属于 reasoning
      reasoning += rest
      break
    }
    reasoning += rest.slice(0, closeIndex)
    rest = rest.slice(closeIndex + THINK_CLOSE.length)
  }

  return { reasoning: reasoning.trim(), visible: visible.trim() }
}
