import type { Method } from 'axios'

import { HttpError, normalizeHttpError } from './errors'
import type { HttpContext, HttpMiddleware } from './types'

/** 将请求链中的异常统一转换为 `HttpError`。 */
export function errorNormalizerMiddleware(): HttpMiddleware {
  return async (context, next) => {
    try {
      await next()
    } catch (error) {
      context.error = normalizeHttpError(error)
      throw context.error
    }
  }
}

/** 自定义业务响应中间件选项。 */
export interface BusinessResponseMiddlewareOptions<TResponse, TResult> {
  type: string
  isSuccess(response: TResponse, context: HttpContext): boolean
  transform(response: TResponse, context: HttpContext): TResult
  getCode?(response: TResponse, context: HttpContext): string | number | undefined
  getMessage?(response: TResponse, context: HttpContext): string
  getStatus?(response: TResponse, context: HttpContext): number | undefined
}

/** 定义由项目控制响应结构、成功规则和返回类型的业务中间件。 */
export function defineBusinessResponseMiddleware<TResponse, TResult>(
  options: BusinessResponseMiddlewareOptions<TResponse, TResult>,
): HttpMiddleware {
  return async (context, next) => {
    await next()
    if (context.config.businessType !== options.type || !context.response) return
    const response = context.response.data as TResponse
    if (!options.isSuccess(response, context)) {
      throw new HttpError({
        type: 'business',
        code: options.getCode?.(response, context),
        message: options.getMessage?.(response, context) ?? '业务请求失败',
        status: options.getStatus?.(response, context),
        cause: response,
      })
    }
    context.data = options.transform(response, context)
  }
}

/** 自动重试配置。 */
export interface RetryOptions {
  count?: number
  methods?: Method[]
  delay?: number
}

/** 为传输层声明幂等请求重试策略。 */
export function retryMiddleware(options: RetryOptions = {}): HttpMiddleware {
  return async (context, next) => {
    context.state ??= {}
    context.state.retryCount = options.count ?? 1
    context.state.retryMethods = options.methods ?? ['GET', 'HEAD']
    context.state.retryDelay = options.delay ?? 0
    await next()
  }
}
