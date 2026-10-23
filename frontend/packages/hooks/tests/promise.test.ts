import { describe, expect, it, vi } from 'vitest'
import { lazyRetryable } from '../src/tools/promise'

describe('lazyRetryable(失败即清缓存的惰性 Promise 单例)', () => {
  it('成功后缓存:多次调用只初始化一次', async () => {
    const init = vi.fn(async () => 'ready')
    const load = lazyRetryable(init)
    await expect(load()).resolves.toBe('ready')
    await expect(load()).resolves.toBe('ready')
    expect(init).toHaveBeenCalledTimes(1)
  })

  it('失败即清缓存:reject 后下次调用重新初始化(UR-003 核心)', async () => {
    const init = vi.fn(async () => {
      if (init.mock.calls.length === 1) throw new Error('chunk 404')
      return 'ready'
    })
    const load = lazyRetryable(init)
    await expect(load()).rejects.toThrow('chunk 404')
    await expect(load()).resolves.toBe('ready')
    expect(init).toHaveBeenCalledTimes(2)
  })

  it('并发调用共享同一个 in-flight Promise', async () => {
    const init = vi.fn(async () => 42)
    const load = lazyRetryable(init)
    const [a, b] = await Promise.all([load(), load()])
    expect(a).toBe(42)
    expect(b).toBe(42)
    expect(init).toHaveBeenCalledTimes(1)
  })

  it('连续失败每次都重新初始化', async () => {
    const init = vi.fn(async () => {
      throw new Error('down')
    })
    const load = lazyRetryable(init)
    await expect(load()).rejects.toThrow('down')
    await expect(load()).rejects.toThrow('down')
    expect(init).toHaveBeenCalledTimes(2)
  })
})
