/**
 * 可重试的惰性 Promise 单例工厂。
 *
 * 「失败 Promise 永久缓存」反模式的统一收敛:模块级缓存 rejected Promise 后,
 * 同会话内重试永远拿到同一个 rejection(如 echarts chunk 发版后旧 hash 404,
 * 图表永久空白)。本工厂在初始化失败时清空缓存,下次调用重新初始化;
 * 成功后缓存结果,并发调用共享同一个 Promise。
 */

export function lazyRetryable<T>(init: () => Promise<T>): () => Promise<T> {
  let cached: Promise<T> | null = null
  return () => {
    if (!cached) {
      cached = init().catch((error: unknown) => {
        cached = null
        throw error
      })
    }
    return cached
  }
}
