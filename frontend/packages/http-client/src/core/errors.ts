import axios from 'axios'

/** HTTP 统一错误分类。 */
export type HttpErrorType = 'network' | 'timeout' | 'cancel' | 'http' | 'business' | 'unknown'

/** HTTP 基础设施抛出的标准错误。 */
export class HttpError extends Error {
  readonly type: HttpErrorType
  readonly code?: string | number
  readonly status?: number
  readonly cause: unknown

  constructor(options: {
    type: HttpErrorType
    message: string
    code?: string | number
    status?: number
    cause?: unknown
  }) {
    super(options.message)
    this.name = 'HttpError'
    this.type = options.type
    this.code = options.code
    this.status = options.status
    this.cause = options.cause
  }
}

/** 判断未知异常是否为标准 HTTP 错误。 */
export function isHttpError(error: unknown): error is HttpError {
  return error instanceof HttpError
}

/** 将 Axios 或未知异常转换为标准 HTTP 错误。 */
export function normalizeHttpError(error: unknown): HttpError {
  if (isHttpError(error)) return error
  if (axios.isCancel(error)) {
    return new HttpError({
      type: 'cancel',
      message: error.message || '请求已取消',
      code: 'ERR_CANCELED',
      cause: error,
    })
  }
  if (axios.isAxiosError(error)) {
    if (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT') {
      return new HttpError({
        type: 'timeout',
        message: error.message || '请求超时',
        code: error.code,
        cause: error,
      })
    }
    if (error.response) {
      const responseData = error.response.data as
        | { message?: unknown; error_code?: unknown }
        | undefined
      const responseMessage = responseData?.message
      const responseErrorCode = responseData?.error_code
      return new HttpError({
        type: 'http',
        message:
          typeof responseMessage === 'string'
            ? responseMessage
            : error.message || `HTTP ${error.response.status}`,
        code: typeof responseErrorCode === 'string' ? responseErrorCode : error.code,
        status: error.response.status,
        cause: error,
      })
    }
    return new HttpError({
      type: 'network',
      message: error.message || '网络连接失败',
      code: error.code,
      cause: error,
    })
  }
  return new HttpError({
    type: 'unknown',
    message: error instanceof Error ? error.message : '未知请求错误',
    cause: error,
  })
}
