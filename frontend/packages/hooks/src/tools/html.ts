/**
 * HTML 文本转义工具(纯字符串实现)。
 *
 * 用于把后端下发的任意字符串安全插入 innerHTML 类 sink:ECharts html 模式
 * tooltip.formatter 的返回值、v-html、富文本拼接等。此前全仓没有任何转义工具,
 * 图表 tooltip 直接把部署名/模型别名拼进模板串,构成存储型 XSS(审查报告 CRIT-01)。
 */

/** 需转义字符与实体:五字符覆盖文本节点与双引号/单引号属性上下文 */
const HTML_ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

/**
 * 转义 HTML 元字符,返回值可安全拼进 innerHTML。
 * null/undefined → 空串,避免字符串化出的 "undefined" 落进 tooltip。
 */
export function escapeHtml(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value).replace(/[&<>"']/g, char => HTML_ESCAPES[char] ?? char)
}
