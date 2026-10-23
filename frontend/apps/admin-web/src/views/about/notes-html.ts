import DOMPurify from 'dompurify'

/**
 * 更新日志富文本消毒:notes 来自官网 manifest 上游,内容不可信,
 * v-html 渲染前必须经此函数白名单消毒。
 * - 标签:仅放行排版载体,脚本/表单/iframe/事件属性全部剥离
 * - style:属性级放行 + 声明级二次过滤(仅颜色与轻排版,值走安全字符集,
 *   url()/expression()/@import 等引用型值显式拒绝),无外链与定位能力
 * - 兼容老式颜色写法:<font color="red">(生成器仍常见输出)
 * - 纯文本输入:整体 HTML 转义后单换行转 <br>,观感与 whitespace-pre-line 一致
 */

/** 放行的 HTML 标签白名单 */
const ALLOWED_TAGS = [
  'p', 'br', 'b', 'strong', 'i', 'em', 'u', 's', 'code', 'pre', 'blockquote',
  'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'a', 'span', 'div',
  'hr', 'img', 'font',
]

/** 放行的属性:href/title(链接)、src/alt(图片)、color(font 颜色)、style(声明级二次过滤) */
const ALLOWED_ATTR = ['href', 'title', 'target', 'rel', 'src', 'alt', 'color', 'style']

/** style 声明属性白名单:颜色与轻排版,无外链/定位/布局能力 */
const STYLE_PROPS = new Set([
  'color',
  'background-color',
  'font-weight',
  'font-style',
  'text-align',
  'text-decoration-line',
  'text-decoration-color',
  'border-color',
  'opacity',
])

/**
 * 声明值安全形态:常见颜色/字重/对齐取值的字符集(十六进制、rgb()/hsla()、
 * 命名色、百分比、数字),不含引号/斜杠/冒号,天然拒绝 url(//..)、@import 等
 */
const SAFE_VALUE_RE = /^[#\w\s(),.%-]{1,100}$/

/** 字符集放行但仍需显式拒绝的引用型/绑定型 token(如无引号 url(evil)) */
const UNSAFE_VALUE_TOKEN_RE = /url|expression|behavior/i

/** 过滤单个元素的 style 声明串;全部不合规时返回空串(调用方移除属性) */
function filterStyleDeclarations(style: string): string {
  const kept: string[] = []
  for (const declaration of style.split(';')) {
    const sep = declaration.indexOf(':')
    if (sep < 0) continue
    const prop = declaration.slice(0, sep).trim().toLowerCase()
    const value = declaration.slice(sep + 1).trim()
    if (STYLE_PROPS.has(prop) && SAFE_VALUE_RE.test(value) && !UNSAFE_VALUE_TOKEN_RE.test(value)) {
      kept.push(`${prop}: ${value}`)
    }
  }
  return kept.join('; ')
}

/** 纯文本 HTML 转义映射 */
const HTML_ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

/** 纯文本输入:转义后单换行转 <br>,保持多行观感 */
function escapePlainText(text: string): string {
  return text
    .replace(/[&<>"']/g, ch => HTML_ESCAPE_MAP[ch] ?? ch)
    .replace(/\n/g, '<br>')
}

/** 消毒更新日志 HTML;输入为空返回空串 */
export function sanitizeNotesHtml(raw: string): string {
  if (!raw) return ''
  const looksLikeHtml = /<\/?[a-z][\s\S]*>/i.test(raw)
  const source = looksLikeHtml ? raw : escapePlainText(raw)

  const sanitized = DOMPurify.sanitize(source, { ALLOWED_TAGS, ALLOWED_ATTR, ALLOW_DATA_ATTR: false })
  if (!sanitized) return ''

  // DOMPurify 不做 CSS 级消毒:二次解析 sanitize 结果,对 style 声明做白名单过滤
  const doc = new DOMParser().parseFromString(`<body>${sanitized}</body>`, 'text/html')
  for (const el of doc.body.querySelectorAll('[style]')) {
    const filtered = filterStyleDeclarations(el.getAttribute('style') ?? '')
    if (filtered) el.setAttribute('style', filtered)
    else el.removeAttribute('style')
  }
  return doc.body.innerHTML
}
