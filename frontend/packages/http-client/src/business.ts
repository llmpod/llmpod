import { defineBusinessResponseMiddleware } from './core/middlewares'
import type { HttpClient, HttpMiddleware, HttpRequestConfig } from './core/types'

/** 统一业务响应信封。 */
export interface ApiEnvelope<T> {
  code: number
  data: T
  message: string
  error_code?: string | null
}

/** 标准分页结果(字段与后端分页协议 docs/api-billing-frontend.md 对齐,snake_case)。 */
export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 仅返回业务消息的操作结果。 */
export interface MessageResult {
  code: number
  message: string
}

/** 同时返回消息和附加数据的操作结果。 */
export interface OperationResult<T> extends MessageResult {
  success: boolean
  data: T
}

/** 带项目业务语义快捷方法的 HTTP 客户端。 */
export interface BusinessHttpClient extends HttpClient {
  data<T = unknown, D = unknown>(
    url: string,
    payload?: D,
    config?: HttpRequestConfig<D>,
  ): Promise<T>
  list<T = unknown, D = unknown>(
    url: string,
    payload?: D,
    config?: HttpRequestConfig<D>,
  ): Promise<T[]>
  page<T = unknown, D = unknown>(
    url: string,
    payload?: D,
    config?: HttpRequestConfig<D>,
  ): Promise<PageResult<T>>
  message<D = unknown>(
    url: string,
    payload?: D,
    config?: HttpRequestConfig<D>,
  ): Promise<MessageResult>
  result<T = unknown, D = unknown>(
    url: string,
    payload?: D,
    config?: HttpRequestConfig<D>,
  ): Promise<OperationResult<T>>
  raw<T = unknown, D = unknown>(url: string, payload?: D, config?: HttpRequestConfig<D>): Promise<T>
}

/** 在核心客户端上组合项目业务语义快捷方法。 */
export function createBusinessHttpClient(core: HttpClient): BusinessHttpClient {
  return Object.assign(core, {
    data: <T, D>(url: string, payload?: D, config = {}) =>
      core.business<T, D>('data', url, payload, config),
    list: <T, D>(url: string, payload?: D, config = {}) =>
      core.business<T[], D>('list', url, payload, config),
    page: <T, D>(url: string, payload?: D, config = {}) =>
      core.business<PageResult<T>, D>('page', url, payload, config),
    message: <D>(url: string, payload?: D, config = {}) =>
      core.business<MessageResult, D>('message', url, payload, config),
    result: <T, D>(url: string, payload?: D, config = {}) =>
      core.business<OperationResult<T>, D>('result', url, payload, config),
    raw: <T, D>(url: string, payload?: D, config = {}) =>
      core.business<T, D>('raw', url, payload, config),
  }) as BusinessHttpClient
}

/** 创建适配 `{ code, data, message, error_code }` 协议的业务响应中间件。 */
export function createBusinessResponseMiddlewares(
  successCodes: number[] = [0, 200],
): HttpMiddleware[] {
  const isSuccess = (response: ApiEnvelope<unknown>) => successCodes.includes(response.code)
  const getCode = (response: ApiEnvelope<unknown>) => response.error_code ?? response.code
  const getMessage = (response: ApiEnvelope<unknown>) => response.message
  // 信封 code 与 HTTP 状态码同语义,400-599 区间映射为 status,驱动 401/403 反馈与刷新令牌流程
  const getStatus = (response: ApiEnvelope<unknown>) =>
    response.code >= 400 && response.code < 600 ? response.code : undefined
  return [
    defineBusinessResponseMiddleware<ApiEnvelope<unknown>, unknown>({
      type: 'data',
      isSuccess,
      getCode,
      getMessage,
      getStatus,
      transform: (response) => response.data,
    }),
    defineBusinessResponseMiddleware<ApiEnvelope<unknown[]>, unknown[]>({
      type: 'list',
      isSuccess,
      getCode,
      getMessage,
      getStatus,
      transform: (response) => response.data,
    }),
    defineBusinessResponseMiddleware<ApiEnvelope<PageResult<unknown>>, PageResult<unknown>>({
      type: 'page',
      isSuccess,
      getCode,
      getMessage,
      getStatus,
      transform: (response) => response.data,
    }),
    defineBusinessResponseMiddleware<ApiEnvelope<unknown>, MessageResult>({
      type: 'message',
      isSuccess,
      getCode,
      getMessage,
      getStatus,
      transform: (response) => ({ code: response.code, message: response.message }),
    }),
    defineBusinessResponseMiddleware<ApiEnvelope<unknown>, OperationResult<unknown>>({
      type: 'result',
      isSuccess,
      getCode,
      getMessage,
      getStatus,
      transform: (response) => ({
        success: true,
        code: response.code,
        message: response.message,
        data: response.data,
      }),
    }),
    defineBusinessResponseMiddleware<unknown, unknown>({
      type: 'raw',
      isSuccess: () => true,
      transform: (response) => response,
    }),
  ]
}
