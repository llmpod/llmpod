import { isHttpError, normalizeHttpError } from './core/errors'
import type { HttpContext, HttpMiddleware } from './core/types'

/** 创建每次请求动态读取访问令牌的认证中间件。 */
export function authMiddleware(getAccessToken: () => string | undefined): HttpMiddleware {
  return async (context, next) => {
    if (context.config.meta?.auth !== false) {
      const token = getAccessToken()
      if (token)
        context.config.headers = { ...context.config.headers, Authorization: `Bearer ${token}` }
    }
    await next()
  }
}

/** 错误反馈中间件依赖。 */
export interface ErrorFeedbackOptions {
  onUnauthorized?(): void
  onForbidden?(): void
  onError?(message: string): void
  unauthorizedLockMs?: number
}

/** 将标准错误路由到应用注入的反馈回调。 */
export function errorFeedbackMiddleware(options: ErrorFeedbackOptions = {}): HttpMiddleware {
  let unauthorizedLocked = false
  return async (context, next) => {
    try {
      await next()
    } catch (error) {
      const normalized = normalizeHttpError(error)
      const skipUnauthorized = context.config.meta?.skipUnauthorized === true
      if (normalized.status === 401 && options.onUnauthorized && !skipUnauthorized) {
        if (!unauthorizedLocked) {
          unauthorizedLocked = true
          options.onUnauthorized()
          globalThis.setTimeout(() => {
            unauthorizedLocked = false
          }, options.unauthorizedLockMs ?? 2_000)
        }
      } else if (normalized.status === 403 && options.onForbidden) {
        options.onForbidden()
      } else if (context.config.meta?.silentError !== true && normalized.type !== 'cancel') {
        options.onError?.(normalized.message)
      }
      throw normalized
    }
  }
}

/** 刷新令牌中间件依赖。 */
export interface RefreshTokenOptions {
  refresh(): Promise<void>
  replay(context: HttpContext): Promise<unknown>
  onRefreshFailed?(error: unknown): void
}

/** 创建共享一次刷新操作的 401 恢复中间件。 */
export function createRefreshTokenMiddleware(options: RefreshTokenOptions): HttpMiddleware {
  let refreshing: Promise<void> | undefined
  return async (context, next) => {
    try {
      await next()
    } catch (error) {
      if (!isHttpError(error) || error.status !== 401 || context.config.meta?.skipRefreshToken)
        throw error
      refreshing ??= options.refresh().finally(() => {
        refreshing = undefined
      })
      try {
        await refreshing
        context.data = await options.replay(context)
      } catch (refreshError) {
        options.onRefreshFailed?.(refreshError)
        throw refreshError
      }
    }
  }
}

/** Loading 状态适配器。 */
export interface LoadingAdapter {
  start(): void
  finish(): void
}

/** Loading 中间件配置。 */
export interface LoadingOptions {
  delay?: number
}

/** 为请求提供延迟显示且支持应用自定义状态管理的 Loading。 */
export function loadingMiddleware(
  adapter: LoadingAdapter,
  options: LoadingOptions = {},
): HttpMiddleware {
  return async (context, next) => {
    if (context.config.meta?.showLoading === false) return next()
    let started = false
    const timer = globalThis.setTimeout(() => {
      started = true
      adapter.start()
    }, options.delay ?? 200)
    try {
      await next()
    } finally {
      globalThis.clearTimeout(timer)
      if (started) adapter.finish()
    }
  }
}
