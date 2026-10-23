import type { AxiosAdapter } from 'axios'

import {
  createBusinessHttpClient,
  createBusinessResponseMiddlewares,
  type BusinessHttpClient,
} from './business'
import { createHttpClient } from './core/client'
import { errorNormalizerMiddleware, retryMiddleware } from './core/middlewares'
import type { HttpRequestConfig } from './core/types'
import { authMiddleware, errorFeedbackMiddleware } from './middlewares'

/** 创建应用请求实例所需依赖。 */
export interface CreateRequestOptions {
  baseURL: string
  getToken: () => string | undefined
  onUnauthorized?: () => void
  onForbidden?: () => void
  onError?: (message: string) => void
  timeout?: number
  withCredentials?: boolean
  adapter?: AxiosAdapter
  successCodes?: number[]
}

/** 兼容现有应用的业务层请求函数。 */
export type Request = <T = unknown>(config: HttpRequestConfig) => Promise<T>

/** 创建兼容请求函数和完整业务客户端。 */
export function createRequest(options: CreateRequestOptions): {
  instance: BusinessHttpClient['instance']
  request: Request
  http: BusinessHttpClient
} {
  const core = createHttpClient({
    defaults: {
      baseURL: options.baseURL,
      timeout: options.timeout ?? 30_000,
      adapter: options.adapter,
      responseMode: 'data',
      headers: { 'Content-Type': 'application/json' },
    },
    policy: {
      values: { withCredentials: options.withCredentials ?? true },
      protectedKeys: ['withCredentials'],
    },
    middlewares: [
      errorFeedbackMiddleware(options),
      errorNormalizerMiddleware(),
      authMiddleware(options.getToken),
      retryMiddleware({ count: 1, methods: ['GET', 'HEAD'] }),
      ...createBusinessResponseMiddlewares(options.successCodes),
    ],
  })
  const http = createBusinessHttpClient(core)
  const request: Request = <T = unknown>(config: HttpRequestConfig) =>
    http.request<T>({ ...config, businessType: 'data' })
  return { instance: http.instance, request, http }
}
