import { toRaw } from 'vue'

/** 虚拟文本单行渲染项（超长物理行会被切分为多个虚拟行，避免单行排版阻塞主线程） */
export interface VirtualTextLine {
  /** 虚拟列表稳定 key */
  key: number
  /** 逻辑行号（1-based）；超长单行切分出的续行为 null */
  lineNo: number | null
  /** 当前虚拟行展示文本（长度不超过 maxLineChars） */
  text: string
  /** 是否为超长单行切分出的续行 */
  wrapped: boolean
}

export interface PrepareVirtualTextOptions {
  /** 单行最大字符数，超出后物理切分为多条虚拟行，默认 240 */
  maxLineChars?: number
  /** 字符串 JSON 自动美化体积上限（字符数），超出直接原样展示避免解析阻塞，默认 512KB */
  prettyLimit?: number
  /** 虚拟行安全上限（防止滚动容器超出浏览器最大高度限制），默认 200_000 */
  maxVirtualLines?: number
}

export interface PreparedVirtualText {
  /** 完整格式化文本（用于剪贴板复制，不受虚拟行截断影响）；空值时为空串 */
  text: string
  /** 原始入参是否为 null / undefined */
  empty: boolean
  /** 是否因大字符串跳过 JSON.parse + pretty-print */
  skippedPretty: boolean
  /** 虚拟行列表是否触及安全上限被截断（复制仍为全文） */
  truncated: boolean
  /** 文本总字符数 */
  charCount: number
  /** 原始逻辑行数 */
  logicalLineCount: number
  /** 切分后的虚拟行数组 */
  lines: VirtualTextLine[]
}

export interface EstimateHeightOptions {
  /** 单行最小高度（px），须与 NVirtualList itemSize 对齐，默认 20 */
  lineHeight?: number
  /** 列表上下内边距（px），须与 NVirtualList paddingTop/paddingBottom 对齐，默认 8 */
  paddingY?: number
  /** 最小容器高度（px），默认 40 */
  minHeight?: number
  /** 最大容器高度（px），默认 240（对应 24rem） */
  maxHeight?: number
  /** 估算软折行时的每行近似字符数，默认 72 */
  charsPerVisualLine?: number
}

export const DEFAULT_MAX_LINE_CHARS = 240
export const DEFAULT_PRETTY_LIMIT = 512 * 1024
export const DEFAULT_MAX_VIRTUAL_LINES = 200_000

/** 判断字符串去除前导空白后是否形如 JSON 对象或数组 */
function looksLikeJsonContainer(input: string): boolean {
  const length = input.length
  let cursor = 0
  while (cursor < length) {
    const code = input.charCodeAt(cursor)
    if (code !== 32 && code !== 9 && code !== 10 && code !== 13) {
      return code === 123 || code === 91 // '{' or '['
    }
    cursor += 1
  }
  return false
}

/** 将请求/响应体统一转为可展示字符串；大字符串跳过 JSON 重解析以防阻塞 */
export function stringifyVirtualContent(
  body: unknown,
  prettyLimit = DEFAULT_PRETTY_LIMIT,
): { text: string; empty: boolean; skippedPretty: boolean } {
  if (body == null) {
    return { text: '', empty: true, skippedPretty: false }
  }
  if (typeof body === 'string') {
    if (!looksLikeJsonContainer(body)) {
      return { text: body, empty: false, skippedPretty: false }
    }
    if (body.length > prettyLimit) {
      return { text: body, empty: false, skippedPretty: true }
    }
    try {
      return {
        text: JSON.stringify(JSON.parse(body), null, 2),
        empty: false,
        skippedPretty: false,
      }
    } catch {
      return { text: body, empty: false, skippedPretty: false }
    }
  }
  if (typeof body === 'object') {
    // 兜底剥离顶层 Vue Proxy，防止调用方误用深层 ref 导致遍历触发依赖收集
    const rawBody = toRaw(body) as { _raw?: unknown }
    if (typeof rawBody._raw === 'string') {
      return { text: rawBody._raw, empty: false, skippedPretty: false }
    }
    try {
      const formatted = JSON.stringify(rawBody, null, 2)
      return {
        text: formatted ?? String(rawBody),
        empty: false,
        skippedPretty: false,
      }
    } catch {
      return { text: String(rawBody), empty: false, skippedPretty: false }
    }
  }
  return { text: String(body), empty: false, skippedPretty: false }
}

/**
 * 按换行符与单行上限切分为虚拟列表项。
 * - 使用 indexOf 顺序扫描，避免 split() 额外分配整份字符串数组；
 * - 切分边界避开 UTF-16 高代理项，防止拆碎 emoji / 增补平面汉字；
 * - 超过 maxVirtualLines 时封顶并标记 truncated，避免触发浏览器最大滚动高度上限。
 */
export function splitTextToVirtualLines(
  text: string,
  options: { maxLineChars?: number; maxVirtualLines?: number } = {},
): { lines: VirtualTextLine[]; logicalLineCount: number; truncated: boolean } {
  const maxLineChars = Math.max(16, options.maxLineChars ?? DEFAULT_MAX_LINE_CHARS)
  const maxVirtualLines = Math.max(1, options.maxVirtualLines ?? DEFAULT_MAX_VIRTUAL_LINES)

  const lines: VirtualTextLine[] = []
  let keySeed = 0
  let lineNo = 1
  let start = 0
  let truncated = false
  const totalLength = text.length

  while (start <= totalLength) {
    let newlineIndex = text.indexOf('\n', start)
    if (newlineIndex === -1) newlineIndex = totalLength

    // 兼容 CRLF（\r\n），剥离行尾 \r
    const lineEnd =
      newlineIndex > start && text.charCodeAt(newlineIndex - 1) === 13
        ? newlineIndex - 1
        : newlineIndex
    const lineLength = lineEnd - start

    if (lineLength <= maxLineChars) {
      if (lines.length >= maxVirtualLines) {
        truncated = true
        break
      }
      lines.push({
        key: keySeed++,
        lineNo,
        text: text.slice(start, lineEnd),
        wrapped: false,
      })
    } else {
      let offset = start
      let isFirstChunk = true
      while (offset < lineEnd) {
        if (lines.length >= maxVirtualLines) {
          truncated = true
          break
        }
        let chunkEnd = Math.min(offset + maxLineChars, lineEnd)
        // 若切点恰好落在 UTF-16 高代理项（0xD800-0xDBFF），回退 1 位保持代理对完整
        if (chunkEnd < lineEnd && chunkEnd - 1 > offset) {
          const tailCode = text.charCodeAt(chunkEnd - 1)
          if (tailCode >= 0xd800 && tailCode <= 0xdbff) {
            chunkEnd -= 1
          }
        }
        lines.push({
          key: keySeed++,
          lineNo: isFirstChunk ? lineNo : null,
          text: text.slice(offset, chunkEnd),
          wrapped: !isFirstChunk,
        })
        offset = chunkEnd
        isFirstChunk = false
      }
      if (truncated) break
    }

    if (newlineIndex === totalLength) break
    lineNo += 1
    start = newlineIndex + 1
  }

  // 若触及上限，额外统计剩余逻辑行数仅需轻量扫描或保留当前统计，尾部追加一条提示行
  if (truncated) {
    lines.push({
      key: keySeed++,
      lineNo: null,
      text: `… 内容过长，已展示前 ${maxVirtualLines.toLocaleString('zh-CN')} 行片段，请点击右上角「复制」查看完整内容`,
      wrapped: true,
    })
  }

  return {
    lines,
    logicalLineCount: lineNo,
    truncated,
  }
}

/** 一步完成数据格式化与虚拟行切分 */
export function prepareVirtualText(
  body: unknown,
  options: PrepareVirtualTextOptions = {},
): PreparedVirtualText {
  const { text, empty, skippedPretty } = stringifyVirtualContent(body, options.prettyLimit)
  if (empty) {
    return {
      text: '',
      empty: true,
      skippedPretty: false,
      truncated: false,
      charCount: 0,
      logicalLineCount: 0,
      lines: [],
    }
  }
  const { lines, logicalLineCount, truncated } = splitTextToVirtualLines(text, {
    maxLineChars: options.maxLineChars,
    maxVirtualLines: options.maxVirtualLines,
  })
  return {
    text,
    empty: false,
    skippedPretty,
    truncated,
    charCount: text.length,
    logicalLineCount,
    lines,
  }
}

/** 格式化字符体积文案（按 UTF-16 字符估算，避免对几 MB 文本做 TextEncoder 全量编码） */
export function formatTextSize(charCount: number): string {
  if (charCount < 1024) return `${charCount} 字符`
  const kb = charCount / 1024
  if (kb < 1024) return `${kb >= 100 ? kb.toFixed(0) : kb.toFixed(1)} KB`
  const mb = kb / 1024
  return `${mb.toFixed(2)} MB`
}

/**
 * 估算虚拟列表容器明确高度（px）：
 * NVirtualList 依赖明确容器高度初始化视口（height:100% 在纯 max-height 下易塌陷为 0）。
 * 仅扫描前 24 条虚拟行（O(1)），短内容保持紧凑高度，长内容封顶 maxHeight。
 */
export function estimateVirtualTextHeight(
  lines: readonly VirtualTextLine[],
  options: EstimateHeightOptions = {},
): number {
  const lineHeight = options.lineHeight ?? 20
  const paddingY = options.paddingY ?? 8
  const minHeight = options.minHeight ?? 40
  const maxHeight = options.maxHeight ?? 240
  const charsPerVisualLine = Math.max(16, options.charsPerVisualLine ?? 72)

  if (lines.length === 0) return minHeight

  const sampleLimit = Math.min(lines.length, 24)
  let visualLines = 0
  const maxVisualLines = Math.ceil((maxHeight - paddingY * 2) / lineHeight)

  for (let index = 0; index < sampleLimit; index += 1) {
    const contentLength = lines[index].text.length
    visualLines += Math.max(1, Math.ceil(contentLength / charsPerVisualLine))
    if (visualLines >= maxVisualLines) {
      return maxHeight
    }
  }

  const estimated = visualLines * lineHeight + paddingY * 2
  return Math.max(minHeight, Math.min(maxHeight, estimated))
}
