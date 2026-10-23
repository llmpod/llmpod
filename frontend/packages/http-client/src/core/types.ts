import type { AxiosInstance, AxiosRequestConfig, AxiosResponse, Method } from 'axios'

/** 请求并发处理模式。 */
export type CancelMode = 'none' | 'latest' | 'first' | 'dedupe'

/** 请求返回值模式。 */
export type ResponseMode = 'data' | 'response'

/** 请求扩展元数据。 */
export interface HttpRequestMeta {
  auth?: boolean
  showLoading?: boolean
  silentError?: boolean
  resolveBusinessResponse?: boolean
  skipRefreshToken?: boolean
  skipUnauthorized?: boolean
  [key: string]: unknown
}

/** 支持中间件和取消策略的请求配置。 */
export interface HttpRequestConfig<D = unknown> extends AxiosRequestConfig<D> {
  businessType?: string
  meta?: HttpRequestMeta
  requestKey?: string
  cancelMode?: CancelMode
  scope?: string
  middlewares?: HttpMiddleware[]
  responseMode?: ResponseMode
}

/** 请求运行时共享状态。 */
export interface HttpRuntimeState {
  retryCount?: number
  retryMethods?: Method[]
  retryDelay?: number
  [key: string]: unknown
}

/** 单次请求的中间件上下文。 */
export interface HttpContext {
  config: HttpRequestConfig
  response?: AxiosResponse
  data?: unknown
  error?: unknown
  state?: HttpRuntimeState
  axios?: AxiosInstance
}

/** 中间件的下一步执行函数。 */
export type HttpNext = () => Promise<void>

/** HTTP 洋葱中间件。 */
export type HttpMiddleware = (context: HttpContext, next: HttpNext) => Promise<void>

/** 不可被单次请求覆盖的全局策略。 */
export interface HttpPolicy {
  values: Partial<HttpRequestConfig>
  protectedKeys: Array<keyof HttpRequestConfig>
}

/** 创建 HTTP 客户端的配置。 */
export interface CreateHttpClientOptions {
  defaults?: HttpRequestConfig
  policy?: HttpPolicy
  middlewares?: HttpMiddleware[]
}

/** 应用使用的 HTTP 客户端接口。 */
export interface HttpClient {
  readonly instance: AxiosInstance
  request<T = unknown, D = unknown>(config: HttpRequestConfig<D>): Promise<T>
  get<T = unknown>(url: string, config?: HttpRequestConfig): Promise<T>
  delete<T = unknown>(url: string, config?: HttpRequestConfig): Promise<T>
  head<T = unknown>(url: string, config?: HttpRequestConfig): Promise<T>
  post<T = unknown, D = unknown>(url: string, data?: D, config?: HttpRequestConfig<D>): Promise<T>
  put<T = unknown, D = unknown>(url: string, data?: D, config?: HttpRequestConfig<D>): Promise<T>
  patch<T = unknown, D = unknown>(url: string, data?: D, config?: HttpRequestConfig<D>): Promise<T>
  business<T = unknown, D = unknown>(
    type: string,
    url: string,
    data?: D,
    config?: HttpRequestConfig<D>,
  ): Promise<T>
  use(middleware: HttpMiddleware): () => void
  cancelByKey(key: string, reason?: string): void
  cancelScope(scope: string, reason?: string): void
  download(url: string, config?: HttpRequestConfig & { filename?: string }): Promise<Blob>
}
