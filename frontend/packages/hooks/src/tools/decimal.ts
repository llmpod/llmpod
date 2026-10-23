/**
 * Decimal 金额字符串展示工具(纯字符串实现)。
 *
 * 金额展示禁用 Number() 中转:小数值经 String(Number(x)) 会转科学计数法
 * ("0.0000005" → "5e-7"),≥1e21 同样,超过 17 位有效数字丢精度
 * (docs/api-billing-frontend.md §0:金额用字符串/Decimal 处理,不用浮点)。
 * 后端 Decimal 字段以定点字符串下发,这里只做字符串层面的尾零剥离与符号归一。
 */

/** 合法十进制定点字符串:可选符号 + 整数 + 可选小数部分(科学计数法等非常规格式原样返回) */
const DECIMAL_RE = /^[+-]?(\d+)(?:\.(\d+))?$/

/**
 * 去尾零并归一符号/前导零;非十进制定点字符串返回 null。
 * "12.500000" → "12.5";"0.000000" → "0";"-0.000000" → "0"(负零归一);"+7.10" → "7.1"
 */
function normalizeDecimal(text: string): string | null {
  const match = DECIMAL_RE.exec(text.trim())
  if (!match) return null
  const [, rawInt, rawFrac] = match
  const negative = text.trim().startsWith('-')
  const intPart = rawInt.replace(/^0+(?=\d)/, '')
  const fracPart = (rawFrac ?? '').replace(/0+$/, '')
  const body = fracPart ? `${intPart}.${fracPart}` : intPart
  return negative && body !== '0' ? `-${body}` : body
}

/**
 * Decimal 字符串去尾零展示("12.500000" → "12.5","0.000000" → "0",
 * "0.0000005" → "0.0000005" 不进科学计数法);空值显示 —;非数字原样返回。
 */
export function formatDecimal(value: string | null | undefined): string {
  if (!value) return '—'
  return normalizeDecimal(value) ?? value
}

/**
 * 变动金额展示:正数加 +,负数保留 -,去尾零("100.000000" → "+100",
 * "-0.012500" → "-0.0125","0.000000" → "0" 不加符号);非数字原样返回。
 */
export function formatSignedAmount(value: string): string {
  const text = normalizeDecimal(value)
  if (text === null) return value
  return text.startsWith('-') || text === '0' ? text : `+${text}`
}
