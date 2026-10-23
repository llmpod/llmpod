import { onScopeDispose, ref, toValue, watch } from 'vue'
import type { MaybeRefOrGetter, Ref } from 'vue'

import { clearCachedRecords, getCachedRecord, putCachedRecord, removeCachedRecord } from './db'

/** 同一 url 的并发请求共享一个 Promise,避免重复 fetch。 */
const inFlight = new Map<string, Promise<Blob>>()

/**
 * 是否当前页面同源(相对路径按当前 origin 解析)。
 * fetch 受 CORS 约束,只有同源 http(s) url 才能安全拉取 blob;
 * <img>/CSS background 是 no-cors 元素请求,跨域图片交给浏览器直接加载。
 */
function isSameOriginUrl(url: string): boolean {
  if (typeof window === 'undefined') return false
  try {
    return new URL(url, window.location.href).origin === window.location.origin
  } catch {
    return false
  }
}

async function loadBlob(url: string): Promise<Blob> {
  const cached = await getCachedRecord(url)
  if (cached?.blob) return cached.blob

  let pending = inFlight.get(url)
  if (!pending) {
    pending = (async () => {
      const response = await fetch(url)
      if (!response.ok) {
        throw new Error(`图片请求失败: ${response.status} ${url}`)
      }
      const blob = await response.blob()
      await putCachedRecord(url, blob)
      return blob
    })()
    inFlight.set(url, pending)
    const clear = () => inFlight.delete(url)
    pending.then(clear, clear)
  }
  return pending
}

export interface UseCachedImageReturn {
  /** 可直接绑 <img src> 的地址:同源命中缓存为 objectURL,非同源为原始 URL;无图或加载失败时为 undefined */
  blobUrl: Ref<string | undefined>
  loading: Ref<boolean>
  error: Ref<Error | undefined>
  /** 强制重新请求并更新缓存 */
  refresh: () => Promise<void>
}

/**
 * 图片缓存 hook:同源 url 先查 IndexedDB,未命中则 fetch 后写入缓存,返回 objectURL。
 * 非同源 url(跨域外链/data: 等)fetch 受 CORS 限制,直接透传原始地址(不缓存)。
 * IndexedDB 不可用时静默降级为直接请求(不缓存、不抛错)。
 */
export function useCachedImage(url: MaybeRefOrGetter<string | undefined>): UseCachedImageReturn {
  const blobUrl = ref<string>()
  const loading = ref(false)
  const error = ref<Error>()

  let currentObjectUrl: string | undefined
  // 竞态序号:只有最后一次触发的加载允许写回状态
  let seq = 0

  function revokeCurrent(): void {
    if (currentObjectUrl) {
      URL.revokeObjectURL(currentObjectUrl)
      currentObjectUrl = undefined
    }
  }

  async function load(target: string | undefined, force = false): Promise<void> {
    const current = ++seq
    revokeCurrent()
    blobUrl.value = undefined
    error.value = undefined
    if (!target) {
      loading.value = false
      return
    }
    loading.value = true
    try {
      // 跨域外链/data: 等非同源地址:fetch 会被 CORS 拦截(图床普遍不回 CORS 头),
      // 跳过缓存直接透传原始地址,不发请求不报错;img 元素加载不受同源策略限制,
      // 二次加载依赖浏览器 HTTP 缓存而非 IndexedDB
      if (!isSameOriginUrl(target)) {
        blobUrl.value = target
        return
      }
      if (force) await removeCachedRecord(target)
      const blob = await loadBlob(target)
      if (current !== seq) return
      currentObjectUrl = URL.createObjectURL(blob)
      blobUrl.value = currentObjectUrl
    }
    catch (err) {
      if (current === seq) {
        error.value = err instanceof Error ? err : new Error(String(err))
      }
    }
    finally {
      if (current === seq) loading.value = false
    }
  }

  // flush: 'sync' 保证 url 变化后同步进入加载态,避免竞态与状态滞后
  watch(() => toValue(url), target => void load(target), { immediate: true, flush: 'sync' })
  onScopeDispose(() => {
    // 卸载时使进行中的加载失效,防止 stale 结果创建无人回收的 objectURL
    seq += 1
    revokeCurrent()
  })

  return {
    blobUrl,
    loading,
    error,
    refresh: () => load(toValue(url), true),
  }
}

/**
 * 移除指定图片的缓存。
 * 已知边界:进行中(in-flight)的同 url 请求完成后仍可能写入缓存,无法取消;
 * 如需强制重取请使用 useCachedImage 返回的 refresh。
 */
export async function removeCachedImage(url: string): Promise<void> {
  inFlight.delete(url)
  await removeCachedRecord(url)
}

/**
 * 清空全部图片缓存。
 * 已知边界:进行中(in-flight)的请求完成后仍可能写入缓存,无法取消;
 * 如需强制重取请使用 useCachedImage 返回的 refresh。
 */
export async function clearCachedImages(): Promise<void> {
  inFlight.clear()
  await clearCachedRecords()
}
