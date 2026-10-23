import axios, { AxiosError, AxiosHeaders, type AxiosAdapter, type AxiosResponse } from 'axios'
import { describe, expect, it, vi } from 'vitest'

import {
  HttpError,
  compose,
  createBusinessHttpClient,
  createHttpClient,
  createRequest,
  defineBusinessResponseMiddleware,
  errorNormalizerMiddleware,
  retryMiddleware,
  type HttpMiddleware,
} from '../src'

function response<T>(data: T, status = 200): AxiosResponse<T> {
  return {
    data,
    status,
    statusText: String(status),
    headers: {},
    config: { headers: {} } as AxiosResponse<T>['config'],
  }
}

describe('HTTP middleware core', () => {
  it('executes middleware as an onion', async () => {
    const calls: string[] = []
    const middleware =
      (name: string): HttpMiddleware =>
      async (_context, next) => {
        calls.push(`${name}:enter`)
        await next()
        calls.push(`${name}:leave`)
      }

    await compose([middleware('a'), middleware('b')])({ config: {} }, async () => {
      calls.push('transport')
    })

    expect(calls).toEqual(['a:enter', 'b:enter', 'transport', 'b:leave', 'a:leave'])
  })

  it('retries recoverable GET failures once', async () => {
    let attempts = 0
    const adapter: AxiosAdapter = async (config) => {
      attempts += 1
      if (attempts === 1) throw new AxiosError('offline', 'ERR_NETWORK', config)
      return response({ ok: true })
    }
    const client = createHttpClient({
      defaults: { adapter },
      middlewares: [errorNormalizerMiddleware(), retryMiddleware({ count: 1 })],
    })

    await expect(client.get('/retry')).resolves.toEqual({ ok: true })
    expect(attempts).toBe(2)
  })

  it('supports latest cancellation and deduplication', async () => {
    const resolvers: Array<(value: AxiosResponse) => void> = []
    const adapter: AxiosAdapter = (config) =>
      new Promise((resolve, reject) => {
        config.signal?.addEventListener?.('abort', () =>
          reject(new axios.CanceledError('canceled')),
        )
        resolvers.push(resolve)
      })
    const client = createHttpClient({
      defaults: { adapter },
      middlewares: [errorNormalizerMiddleware()],
    })
    const first = client.get('/search', { requestKey: 'search', cancelMode: 'latest' })
    const second = client.get('/search', { requestKey: 'search', cancelMode: 'latest' })
    resolvers[1]!(response({ value: 2 }))

    await expect(first).rejects.toMatchObject({ type: 'cancel' })
    await expect(second).resolves.toEqual({ value: 2 })

    const sharedA = client.get('/detail', { requestKey: 'detail', cancelMode: 'dedupe' })
    const sharedB = client.get('/detail', { requestKey: 'detail', cancelMode: 'dedupe' })
    expect(sharedA).toBe(sharedB)
    resolvers[2]!(response({ id: '1' }))
    await expect(sharedA).resolves.toEqual({ id: '1' })
  })
})

describe('business methods', () => {
  it('transforms a declared business response type', async () => {
    const adapter: AxiosAdapter = async () =>
      response({ code: 0, data: { id: '1' }, message: 'ok' })
    const middleware = defineBusinessResponseMiddleware<
      { code: number; data: { id: string }; message: string },
      { id: string }
    >({
      type: 'data',
      isSuccess: (value) => value.code === 0,
      getCode: (value) => value.code,
      getMessage: (value) => value.message,
      transform: (value) => value.data,
    })
    const client = createBusinessHttpClient(
      createHttpClient({
        defaults: { adapter },
        middlewares: [errorNormalizerMiddleware(), middleware],
      }),
    )

    await expect(client.data<{ id: string }>('/detail')).resolves.toEqual({ id: '1' })
  })
})

describe('createRequest compatibility facade', () => {
  it('injects the latest token and accepts business success codes 0 and 200', async () => {
    let token = 'old'
    const adapter = vi.fn<AxiosAdapter>(async (config) => {
      expect(config.headers?.Authorization).toBe(`Bearer ${token}`)
      expect(config.method).toBe('get')
      return response({ code: 200, data: { token }, message: 'ok' })
    })
    const { request } = createRequest({ baseURL: '/api', getToken: () => token, adapter })

    await expect(request<{ token: string }>({ url: '/one', method: 'GET' })).resolves.toEqual({
      token: 'old',
    })
    token = 'new'
    await expect(request<{ token: string }>({ url: '/two', method: 'GET' })).resolves.toEqual({
      token: 'new',
    })
  })

  it('normalizes business and HTTP errors while preserving callbacks', async () => {
    const onUnauthorized = vi.fn()
    const onError = vi.fn()
    const businessAdapter: AxiosAdapter = async () =>
      response({ code: 4001, data: null, message: 'invalid' })
    const business = createRequest({
      baseURL: '/api',
      getToken: () => '',
      onError,
      adapter: businessAdapter,
    })
    await expect(business.request({ url: '/invalid' })).rejects.toMatchObject({
      type: 'business',
      code: 4001,
    })
    expect(onError).toHaveBeenCalledWith('invalid')

    const unauthorizedAdapter: AxiosAdapter = async (config) => {
      throw new AxiosError(
        'unauthorized',
        'ERR_BAD_RESPONSE',
        { ...config, headers: new AxiosHeaders(config.headers) },
        undefined,
        response({}, 401),
      )
    }
    const unauthorized = createRequest({
      baseURL: '/api',
      getToken: () => '',
      onUnauthorized,
      adapter: unauthorizedAdapter,
    })
    await expect(unauthorized.request({ url: '/private' })).rejects.toBeInstanceOf(HttpError)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('carries error_code and maps envelope code to status for auth flows', async () => {
    const onUnauthorized = vi.fn()
    const adapter: AxiosAdapter = async () =>
      response({ code: 401, data: null, message: 'Invalid username or password', error_code: 'INVALID_CREDENTIALS' })
    const { request } = createRequest({
      baseURL: '/api',
      getToken: () => '',
      onUnauthorized,
      adapter,
    })

    await expect(request({ url: '/login' })).rejects.toMatchObject({
      type: 'business',
      code: 'INVALID_CREDENTIALS',
      status: 401,
      message: 'Invalid username or password',
    })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('falls back to onError when unauthorized is skipped or unhandled', async () => {
    const onError = vi.fn()
    const adapter: AxiosAdapter = async () =>
      response({ code: 401, data: null, message: 'Invalid username or password', error_code: 'INVALID_CREDENTIALS' })

    // 未注入 onUnauthorized 时,401 回退为 onError 消息提示
    const unhandled = createRequest({ baseURL: '/api', getToken: () => '', onError, adapter })
    await expect(unhandled.request({ url: '/login' })).rejects.toMatchObject({ status: 401 })
    expect(onError).toHaveBeenCalledWith('Invalid username or password')

    // meta.skipUnauthorized 显式跳过 401 处理,同样回退 onError
    const onUnauthorized = vi.fn()
    const skipped = createRequest({
      baseURL: '/api',
      getToken: () => '',
      onError,
      onUnauthorized,
      adapter,
    })
    await expect(
      skipped.request({ url: '/login', meta: { skipUnauthorized: true } }),
    ).rejects.toMatchObject({ status: 401 })
    expect(onUnauthorized).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledTimes(2)
  })
})
