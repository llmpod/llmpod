import type { PlaygroundErrorDetail } from './types'

/** OpenAI SDK APIError 的鸭子类型形状(标准错误体字段已由 SDK 提升到顶层)。 */
interface ApiErrorLike {
  /** HTTP 状态码;网络错误等场景缺省。 */
  status?: number
  /** SDK 从标准错误体 { error: {...} } 提升的字段。 */
  type?: string | null
  code?: string | null
  param?: string | null
  /** OpenAI 标准错误体的内层对象(SDK APIError 与 chat-sender 的 GatewayError 均挂载)。 */
  error?: unknown
  /** 网络错误(SDK APIConnectionError)的底层原因,如 Failed to fetch。 */
  cause?: unknown
}

export interface PlaygroundErrorInfo {
  /** 错误原因主文案:优先 OpenAI 标准错误体的 error.message。 */
  message: string
  /** OpenAI 标准错误详情;非 API 错误(本地错误/网络错误)时为 null。 */
  detail: PlaygroundErrorDetail | null
}

/** 判断值是否为普通对象(排除 null/数组)。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 读取非空字符串,null/undefined 归一为 undefined。 */
function toText(value: unknown): string | undefined {
  return typeof value === 'string' && value !== '' ? value : undefined
}

/**
 * 从抛出的错误中解析 OpenAI 标准错误信息:
 * - SDK APIError:标准错误体已提升出 status/type/code/param,message 为 `${status} ${内层 message}`;
 * - 网关 200/SSE 错误帧:错误体挂在 error 字段(chat-sender 的 GatewayError);
 * - 网络错误:文案固定为 Connection error.,把 cause 底层原因并入文案;
 * - 其余按普通 Error 的 message 处理。
 */
export function parsePlaygroundError(error: unknown): PlaygroundErrorInfo {
  if (!(error instanceof Error)) return { message: '请求失败,请稍后重试', detail: null }

  const api = error as Error & ApiErrorLike
  const body = isRecord(api.error) ? api.error : undefined
  const innerMessage = toText(body?.message)

  let message = innerMessage ?? api.message ?? '请求失败,请稍后重试'
  if (!innerMessage && api.cause instanceof Error && api.cause.message !== '') {
    message = `${message}(${api.cause.message})`
  }

  const status = typeof api.status === 'number' ? api.status : undefined
  const type = toText(api.type) ?? toText(body?.type)
  const code = toText(api.code) ?? toText(body?.code)
  const param = toText(api.param) ?? toText(body?.param)

  const hasDetail =
    status !== undefined || type !== undefined || code !== undefined || param !== undefined
  return { message, detail: hasDetail ? { status, type, code, param } : null }
}
