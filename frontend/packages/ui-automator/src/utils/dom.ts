/**
 * DOM 查找与操作工具函数
 * @description 提供元素查找、可见性检测、位置计算、事件分发等功能
 */

import type { ElementRect, Selector } from '../types'

// ============================================================
// 元素查找
// ============================================================

/**
 * 通过 CSS 选择器或 XPath 字符串查找 DOM 元素
 * @param selector CSS 选择器字符串（以 / 或 ( 开头则视为 XPath）
 * @returns 找到的元素或 null
 */
export function queryElementByString(selector: string): Element | null {
  if (selector.startsWith('/') || selector.startsWith('(')) {
    // XPath 查询
    try {
      const result = document.evaluate(selector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null)
      return result.singleNodeValue as Element | null
    } catch {
      return null
    }
  }
  // CSS 选择器
  return document.querySelector(selector)
}

/**
 * 根据 Selector 类型查找元素，支持字符串/Element
 * @throws 当找不到元素时抛出错误
 */
export function findElement(selector: Selector): HTMLElement {
  let el: Element | null = null

  if (typeof selector === 'string') {
    el = queryElementByString(selector)
    if (!el) {
      throw new Error(`UIAutomator: 找不到元素 "${selector}"`)
    }
  } else if (selector instanceof Element) {
    el = selector
  } else {
    throw new Error(`UIAutomator: 无效的 selector 类型`)
  }

  return el as HTMLElement
}

/**
 * 尝试查找元素，找不到返回 null（不抛错）
 */
export function tryFindElement(selector: Selector): HTMLElement | null {
  try {
    return findElement(selector)
  } catch {
    return null
  }
}

// ============================================================
// 元素状态检测
// ============================================================

/**
 * 检测元素是否可见（CSS 可见性 + 尺寸）
 */
export function isVisible(el: Element): boolean {
  const style = window.getComputedStyle(el)
  if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
    return false
  }
  const rect = el.getBoundingClientRect()
  return rect.width > 0 || rect.height > 0
}

/**
 * 检测元素是否在视口内
 */
export function isInViewport(el: Element): boolean {
  const rect = el.getBoundingClientRect()
  return rect.top >= 0 && rect.left >= 0 && rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) && rect.right <= (window.innerWidth || document.documentElement.clientWidth)
}

// ============================================================
// 元素位置与尺寸
// ============================================================

/**
 * 获取元素在页面中的完整矩形信息（含中心点坐标）
 */
export function getElementRect(el: Element): ElementRect {
  const rect = el.getBoundingClientRect()
  return {
    x: rect.left + window.scrollX,
    y: rect.top + window.scrollY,
    width: rect.width,
    height: rect.height,
    centerX: rect.left + rect.width / 2,
    centerY: rect.top + rect.height / 2
  }
}

/**
 * 获取元素中心点相对于视口的坐标
 */
export function getElementCenter(el: Element): { x: number; y: number } {
  const rect = el.getBoundingClientRect()
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2
  }
}

/**
 * 如果元素不在视口内，滚动使其进入视野
 */
export function scrollIntoViewIfNeeded(el: Element): void {
  if (!isInViewport(el)) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
}

// ============================================================
// 事件分发
// ============================================================

/**
 * 解析可用于构造 UIEvent 的 view（Window）对象
 * 部分多 realm 环境（如 vitest jsdom）中全局 window 无法通过 jsdom 内部的 Window 类型检查，
 * 传入会报 "member view is not of type Window"，故首次调用时探测一次并缓存结果；
 * 不可用时降级为 null（view 默认为 null，不影响事件分发）
 */
let resolvedEventView: Window | null | undefined

function resolveEventView(): Window | null {
  if (resolvedEventView !== undefined) return resolvedEventView
  const candidate = document.defaultView
  try {
    new MouseEvent('viewprobe', { view: candidate })
    resolvedEventView = candidate
  } catch {
    resolvedEventView = null
  }
  return resolvedEventView
}

/** 鼠标事件选项 */
export interface MouseEventInit2 {
  clientX?: number
  clientY?: number
  screenX?: number
  screenY?: number
  button?: number
  buttons?: number
  bubbles?: boolean
  cancelable?: boolean
  ctrlKey?: boolean
  altKey?: boolean
  shiftKey?: boolean
  metaKey?: boolean
  relatedTarget?: EventTarget | null
}

/**
 * 分发鼠标事件
 */
export function dispatchMouseEvent(el: EventTarget, type: string, options: MouseEventInit2 = {}): void {
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    view: resolveEventView(),
    button: 0,
    buttons: 1,
    ...options
  })
  el.dispatchEvent(event)
}

/** 键盘事件选项 */
export interface KeyEventOptions {
  key: string
  code?: string
  keyCode?: number
  charCode?: number
  bubbles?: boolean
  cancelable?: boolean
  ctrlKey?: boolean
  altKey?: boolean
  shiftKey?: boolean
  metaKey?: boolean
}

/**
 * 分发键盘事件
 */
export function dispatchKeyEvent(el: EventTarget, type: 'keydown' | 'keypress' | 'keyup', options: KeyEventOptions): void {
  const event = new KeyboardEvent(type, {
    bubbles: true,
    cancelable: true,
    view: resolveEventView(),
    key: options.key,
    code: options.code ?? `Key${options.key.toUpperCase()}`,
    keyCode: options.keyCode ?? options.key.charCodeAt(0),
    charCode: type === 'keypress' ? (options.charCode ?? options.key.charCodeAt(0)) : 0,
    ctrlKey: options.ctrlKey ?? false,
    altKey: options.altKey ?? false,
    shiftKey: options.shiftKey ?? false,
    metaKey: options.metaKey ?? false
  })
  el.dispatchEvent(event)
}

/**
 * 通过 Object.getOwnPropertyDescriptor 获取原生 value setter
 * 用于兼容 Vue/React 的双向绑定（直接修改 .value 无法触发框架更新）
 */
export function getNativeInputSetter(el: HTMLInputElement | HTMLTextAreaElement): ((value: string) => void) | null {
  // 优先尝试 HTMLInputElement 原型链上的 setter
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype

  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value')
  if (descriptor?.set) {
    return descriptor.set.bind(el)
  }
  return null
}

/**
 * 分发 input 事件（触发 Vue v-model / React onChange）
 */
export function dispatchInputEvent(el: EventTarget): void {
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

/**
 * 分发 change 事件
 */
export function dispatchChangeEvent(el: EventTarget): void {
  el.dispatchEvent(new Event('change', { bubbles: true }))
}

/**
 * 将焦点移动到指定元素
 */
export function focusElement(el: HTMLElement): void {
  el.focus({ preventScroll: true })
}

/**
 * 移除指定元素的焦点
 */
export function blurElement(el: HTMLElement): void {
  el.blur()
}
