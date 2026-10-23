// @vitest-environment happy-dom
import { effectScope, nextTick, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../src/image-cache/db', () => ({
  getCachedRecord: vi.fn(),
  putCachedRecord: vi.fn(),
  removeCachedRecord: vi.fn(),
  clearCachedRecords: vi.fn(),
}))

import { clearCachedRecords, getCachedRecord, putCachedRecord, removeCachedRecord } from '../src/image-cache/db'
import {
  clearCachedImages,
  removeCachedImage,
  useCachedImage,
} from '../src/image-cache/useCachedImage'

function makeBlob(): Blob {
  return new Blob(['fake-image'], { type: 'image/png' })
}

function mockFetchOk(blob = makeBlob()) {
  return vi.fn(async () => ({ ok: true, status: 200, blob: async () => blob }))
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('fetch', mockFetchOk())
  let seq = 0
  // 测试环境无 URL.createObjectURL(happy-dom/Node 均缺),以 mock 替换;
  // 继承真实 URL 保留构造器(hook 内 isSameOriginUrl 依赖 new URL 判断同源性)
  class URLWithObjectUrl extends URL {}
  URLWithObjectUrl.createObjectURL = vi.fn(() => `blob:mock-${++seq}`)
  URLWithObjectUrl.revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', URLWithObjectUrl)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useCachedImage 缓存与加载', () => {
  it('缓存命中时不发 fetch,直接建 objectURL', async () => {
    const blob = makeBlob()
    vi.mocked(getCachedRecord).mockResolvedValue({ blob, updatedAt: Date.now() })
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)

    const { blobUrl, loading } = useCachedImage('/cdn/x.png')
    expect(loading.value).toBe(true)
    await vi.waitFor(() => expect(loading.value).toBe(false))

    expect(fetchSpy).not.toHaveBeenCalled()
    expect(blobUrl.value).toBe('blob:mock-1')
  })

  it('缓存未命中 fetch 后写入缓存', async () => {
    vi.mocked(getCachedRecord).mockResolvedValue(undefined)
    const { blobUrl, loading } = useCachedImage('/cdn/x.png')
    await vi.waitFor(() => expect(loading.value).toBe(false))

    expect(fetch).toHaveBeenCalledWith('/cdn/x.png')
    expect(putCachedRecord).toHaveBeenCalledWith('/cdn/x.png', expect.any(Blob))
    expect(blobUrl.value).toMatch(/^blob:mock-/)
  })

  it('同源绝对 url 正常走 fetch + 缓存', async () => {
    vi.mocked(getCachedRecord).mockResolvedValue(undefined)
    const absolute = new URL('/cdn/abs.png', window.location.href).href
    const { blobUrl } = useCachedImage(absolute)
    await vi.waitFor(() => expect(blobUrl.value).toMatch(/^blob:mock-/))

    expect(fetch).toHaveBeenCalledWith(absolute)
    expect(putCachedRecord).toHaveBeenCalledWith(absolute, expect.any(Blob))
  })

  it('跨域 url 不 fetch 不缓存,直接透传原始地址', async () => {
    const crossOrigin = new URL('/x.png', 'https://cdn.example.com').href
    const { blobUrl, loading, error } = useCachedImage(crossOrigin)
    await nextTick()

    expect(fetch).not.toHaveBeenCalled()
    expect(getCachedRecord).not.toHaveBeenCalled()
    expect(putCachedRecord).not.toHaveBeenCalled()
    expect(blobUrl.value).toBe(crossOrigin)
    expect(loading.value).toBe(false)
    expect(error.value).toBeUndefined()
  })

  it('data: url 直接透传,不走缓存', async () => {
    const dataUrl = 'data:image/png;base64,AAA'
    const { blobUrl } = useCachedImage(dataUrl)
    await nextTick()

    expect(fetch).not.toHaveBeenCalled()
    expect(getCachedRecord).not.toHaveBeenCalled()
    expect(blobUrl.value).toBe(dataUrl)
  })

  it('url 为 undefined 不请求,loading 为 false', async () => {
    const { blobUrl, loading } = useCachedImage(undefined)
    await nextTick()
    expect(fetch).not.toHaveBeenCalled()
    expect(loading.value).toBe(false)
    expect(blobUrl.value).toBeUndefined()
  })

  it('请求失败写入 error,objectURL 为空', async () => {
    vi.mocked(getCachedRecord).mockResolvedValue(undefined)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404 })))
    const { blobUrl, error, loading } = useCachedImage('/cdn/miss.png')
    await vi.waitFor(() => expect(loading.value).toBe(false))

    expect(error.value?.message).toContain('404')
    expect(blobUrl.value).toBeUndefined()
  })

  it('同 url 并发加载共享 in-flight,只发一次 fetch', async () => {
    vi.mocked(getCachedRecord).mockResolvedValue(undefined)
    const a = useCachedImage('/cdn/same.png')
    const b = useCachedImage('/cdn/same.png')
    await vi.waitFor(() => expect(a.loading.value).toBe(false))
    await vi.waitFor(() => expect(b.loading.value).toBe(false))

    expect(fetch).toHaveBeenCalledTimes(1)
    expect(putCachedRecord).toHaveBeenCalledTimes(1)
    expect(a.blobUrl.value).toBeTruthy()
    expect(b.blobUrl.value).toBeTruthy()
  })

  it('url 变更触发重载,旧的 objectURL 被 revoke', async () => {
    vi.mocked(getCachedRecord).mockResolvedValue(undefined)
    const url = ref('/cdn/a.png')
    const { blobUrl } = useCachedImage(url)
    await vi.waitFor(() => expect(blobUrl.value).toBeTruthy())
    const first = blobUrl.value

    url.value = '/cdn/b.png'
    await vi.waitFor(() => expect(blobUrl.value).not.toBe(first))

    expect(URL.revokeObjectURL).toHaveBeenCalledWith(first)
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('竞态:快速变更 url,慢的旧加载不覆盖新结果', async () => {
    vi.mocked(getCachedRecord).mockResolvedValue(undefined)
    const slowBlob = makeBlob()
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string) => {
        if (input.endsWith('slow.png')) {
          return new Promise((resolve) =>
            setTimeout(() => resolve({ ok: true, blob: async () => slowBlob }), 30),
          )
        }
        return Promise.resolve({ ok: true, blob: async () => makeBlob() })
      }),
    )
    const url = ref('/cdn/slow.png')
    const { blobUrl } = useCachedImage(url)
    url.value = '/cdn/fast.png'
    await vi.waitFor(() => expect(blobUrl.value).toBeTruthy(), { timeout: 500 })
    const fastUrl = blobUrl.value

    // 慢响应 30ms 后返回,不应覆盖 fast 结果
    await new Promise((resolve) => setTimeout(resolve, 80))
    expect(blobUrl.value).toBe(fastUrl)
  })

  it('refresh 强制清缓存重取', async () => {
    vi.mocked(getCachedRecord).mockResolvedValue(undefined)
    const { refresh, loading } = useCachedImage('/cdn/r.png')
    await vi.waitFor(() => expect(loading.value).toBe(false))
    expect(fetch).toHaveBeenCalledTimes(1)

    await refresh()
    expect(removeCachedRecord).toHaveBeenCalledWith('/cdn/r.png')
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('作用域销毁后,在途加载的结果不写回', async () => {
    vi.mocked(getCachedRecord).mockResolvedValue(undefined)
    const scope = effectScope()
    let state!: ReturnType<typeof useCachedImage>
    scope.run(() => {
      state = useCachedImage('/cdn/d.png')
    })
    scope.stop()
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(state.blobUrl.value).toBeUndefined()
  })
})

describe('缓存清理 API', () => {
  it('removeCachedImage 删指定记录,clearCachedImages 清空', async () => {
    await removeCachedImage('/cdn/a.png')
    expect(removeCachedRecord).toHaveBeenCalledWith('/cdn/a.png')

    await clearCachedImages()
    expect(clearCachedRecords).toHaveBeenCalled()
  })
})
