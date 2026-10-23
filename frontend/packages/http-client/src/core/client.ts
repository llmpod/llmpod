import axios, { type AxiosRequestConfig, type AxiosResponse } from 'axios'

import { compose } from './compose'
import { normalizeHttpError } from './errors'
import { combineSignals } from './signal'
import type {
  CreateHttpClientOptions,
  HttpClient,
  HttpContext,
  HttpMiddleware,
  HttpRequestConfig,
} from './types'

interface ActiveRequest {
  controller: AbortController
  promise: Promise<unknown>
  scope?: string
}

function mergeConfig(defaults: HttpRequestConfig, request: HttpRequestConfig): HttpRequestConfig {
  return {
    ...defaults,
    ...request,
    headers: { ...defaults.headers, ...request.headers },
    meta: { ...defaults.meta, ...request.meta },
  }
}

function canRetry(error: unknown, method: string | undefined, methods: string[]): boolean {
  const normalized = normalizeHttpError(error)
  if (!methods.map((value) => value.toUpperCase()).includes((method ?? 'GET').toUpperCase()))
    return false
  return (
    normalized.type === 'network' ||
    normalized.type === 'timeout' ||
    (normalized.type === 'http' && (normalized.status ?? 0) >= 500)
  )
}

function delay(duration: number, signal?: AbortSignal): Promise<void> {
  if (duration <= 0) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(resolve, duration)
    signal?.addEventListener(
      'abort',
      () => {
        globalThis.clearTimeout(timer)
        reject(new axios.CanceledError(String(signal.reason ?? 'canceled')))
      },
      { once: true },
    )
  })
}

/** 创建具备中间件、策略和取消管理能力的 HTTP 客户端。 */
export function createHttpClient(options: CreateHttpClientOptions = {}): HttpClient {
  const instance = axios.create()
  const middlewares = [...(options.middlewares ?? [])]
  const active = new Map<string, ActiveRequest>()

  const request = <T = unknown, D = unknown>(requestConfig: HttpRequestConfig<D>): Promise<T> => {
    const config = mergeConfig(options.defaults ?? {}, requestConfig as HttpRequestConfig)
    const key = config.requestKey
    const mode = config.cancelMode ?? 'none'
    const previous = key ? active.get(key) : undefined
    if (previous && (mode === 'dedupe' || mode === 'first')) return previous.promise as Promise<T>
    if (previous && mode === 'latest') previous.controller.abort('由更新的请求替代')

    const controller = new AbortController()
    config.signal = combineSignals([config.signal as AbortSignal | undefined, controller.signal])
    const localMiddlewares = config.middlewares ?? []
    delete config.middlewares
    const context: HttpContext = { config, state: {}, axios: instance }
    const execute = compose([...middlewares, ...localMiddlewares])
    const promise = execute(context, async () => {
      if (options.policy) {
        for (const protectedKey of options.policy.protectedKeys) {
          if (protectedKey in options.policy.values) {
            ;(context.config as Record<string, unknown>)[protectedKey] =
              options.policy.values[protectedKey]
          }
        }
      }
      const runtime = (context.state ??= {})
      const retries = runtime.retryCount ?? 0
      const methods = runtime.retryMethods ?? ['GET', 'HEAD']
      let attempt = 0
      while (true) {
        try {
          context.response = (await instance.request(
            context.config as AxiosRequestConfig,
          )) as AxiosResponse
          context.data = context.response.data
          return
        } catch (error) {
          if (attempt >= retries || !canRetry(error, context.config.method, methods)) throw error
          attempt += 1
          await delay(runtime.retryDelay ?? 0, context.config.signal as AbortSignal | undefined)
        }
      }
    }).then(
      () => (context.config.responseMode === 'response' ? context.response : context.data) as T,
    )

    const tracked = promise.finally(() => {
      if (key && active.get(key)?.promise === tracked) active.delete(key)
    })
    if (key) active.set(key, { controller, promise: tracked, scope: config.scope })
    return tracked
  }

  return {
    instance,
    request,
    get: (url, config = {}) => request({ ...config, url, method: 'GET' }),
    delete: (url, config = {}) => request({ ...config, url, method: 'DELETE' }),
    head: (url, config = {}) => request({ ...config, url, method: 'HEAD' }),
    post: (url, data, config = {}) => request({ ...config, url, data, method: 'POST' }),
    put: (url, data, config = {}) => request({ ...config, url, data, method: 'PUT' }),
    patch: (url, data, config = {}) => request({ ...config, url, data, method: 'PATCH' }),
    business: (type, url, data, config = {}) =>
      request({ ...config, businessType: type, url, data, method: 'POST' }),
    use(middleware: HttpMiddleware) {
      middlewares.push(middleware)
      return () => {
        const index = middlewares.indexOf(middleware)
        if (index >= 0) middlewares.splice(index, 1)
      }
    },
    cancelByKey(key, reason) {
      active.get(key)?.controller.abort(reason)
    },
    cancelScope(scope, reason) {
      for (const entry of active.values()) if (entry.scope === scope) entry.controller.abort(reason)
    },
    async download(url, downloadConfig = {}) {
      const { filename, ...config } = downloadConfig
      const blob = await request<Blob>({
        ...config,
        url,
        method: 'GET',
        responseType: 'blob',
        meta: { ...config.meta, resolveBusinessResponse: false },
      })
      if (typeof document !== 'undefined' && filename) {
        const objectUrl = URL.createObjectURL(blob)
        const anchor = document.createElement('a')
        anchor.href = objectUrl
        anchor.download = filename
        anchor.click()
        URL.revokeObjectURL(objectUrl)
      }
      return blob
    },
  }
}
