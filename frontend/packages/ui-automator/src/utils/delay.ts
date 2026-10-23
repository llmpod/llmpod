/**
 * 延迟与等待工具函数
 * @description 提供 sleep、randomDelay、waitForElement、waitForCondition 等功能
 */

import type { WaitForOptions } from '../types'
import { isVisible, queryElementByString } from './dom'

/**
 * 固定延迟
 * @param ms 延迟毫秒数
 */
export function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

/**
 * 随机延迟
 * @param range [最小毫秒, 最大毫秒]
 */
export function randomDelay(range: [number, number]): Promise<void> {
  const [min, max] = range
  const ms = Math.floor(Math.random() * (max - min + 1)) + min
  return sleep(ms)
}

/**
 * 在某个值上添加随机噪声扰动（用于人性化速度）
 * @param value 原始值
 * @param variance 扰动比例 0-1（默认 0.2，即 ±20%）
 */
export function addNoise(value: number, variance = 0.2): number {
  const delta = value * variance
  return Math.max(0, value + (Math.random() * 2 - 1) * delta)
}

/** 等待元素出现/消失选项（内含默认值） */
const DEFAULT_WAIT_OPTIONS: Required<WaitForOptions> = {
  timeout: 10000,
  interval: 100,
  visible: true
}

/**
 * 等待 DOM 元素出现或消失
 * @param selector CSS 选择器或 XPath
 * @param options 等待选项
 * @returns 找到的元素（当 visible=false 时返回 null）
 * @throws 超时时抛出错误
 */
export function waitForElement(selector: string, options?: WaitForOptions): Promise<Element | null> {
  const opts = { ...DEFAULT_WAIT_OPTIONS, ...options }
  const { timeout, interval, visible } = opts

  return new Promise((resolve, reject) => {
    // 优先使用 MutationObserver，降低性能开销
    let observer: MutationObserver | null = null
    let timeoutId: ReturnType<typeof setTimeout> | null = null
    let intervalId: ReturnType<typeof setInterval> | null = null

    const cleanup = (): void => {
      if (observer) {
        observer.disconnect()
        observer = null
      }
      if (timeoutId !== null) {
        clearTimeout(timeoutId)
        timeoutId = null
      }
      if (intervalId !== null) {
        clearInterval(intervalId)
        intervalId = null
      }
    }

    /** 检测是否满足条件 */
    const check = (): boolean => {
      const el = queryElementByString(selector)
      if (visible) {
        // 等待元素出现且可见
        if (el && isVisible(el)) {
          cleanup()
          resolve(el)
          return true
        }
      } else {
        // 等待元素消失或不可见
        if (!el || !isVisible(el)) {
          cleanup()
          resolve(null)
          return true
        }
      }
      return false
    }

    // 立即检测一次
    if (check()) return

    // 超时处理
    timeoutId = setTimeout(() => {
      cleanup()
      reject(new Error(`waitForElement: 等待 "${selector}" ${visible ? '出现' : '消失'} 超时（${timeout}ms）`))
    }, timeout)

    // 使用 MutationObserver 监听 DOM 变更
    try {
      observer = new MutationObserver(() => {
        check()
      })
      observer.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['style', 'class', 'hidden']
      })
    } catch {
      // 降级为 setInterval
      observer = null
      intervalId = setInterval(() => {
        check()
      }, interval)
    }
  })
}

/**
 * 等待自定义条件成立
 * @param conditionFn 条件函数，返回 true 时认为条件成立
 * @param options 等待选项
 */
export function waitForCondition(conditionFn: () => boolean | Promise<boolean>, options?: WaitForOptions): Promise<void> {
  const opts = { ...DEFAULT_WAIT_OPTIONS, ...options }
  const { timeout, interval } = opts

  return new Promise((resolve, reject) => {
    let timeoutId: ReturnType<typeof setTimeout> | null = null
    let intervalId: ReturnType<typeof setInterval> | null = null

    const cleanup = (): void => {
      if (timeoutId !== null) clearTimeout(timeoutId)
      if (intervalId !== null) clearInterval(intervalId)
    }

    const check = async (): Promise<void> => {
      try {
        const result = await conditionFn()
        if (result) {
          cleanup()
          resolve()
        }
      } catch (err) {
        cleanup()
        reject(err)
      }
    }

    timeoutId = setTimeout(() => {
      cleanup()
      reject(new Error(`waitForCondition: 条件等待超时（${timeout}ms）`))
    }, timeout)

    intervalId = setInterval(check, interval)
    // 立即执行一次
    void check()
  })
}
