/**
 * 鼠标模拟器
 * @description 模拟真实人类鼠标移动（贝塞尔曲线轨迹）、点击、悬停、拖拽等操作
 * 集成 VisualCursor 实现可视化光标效果
 */

import type { Point, MouseSimulatorOptions, BezierPathOptions, Selector } from '../types'
import { generateMousePath, calculateStepDuration } from '../utils/bezier'
import { sleep } from '../utils/delay'
import { findElement, getElementCenter, scrollIntoViewIfNeeded, dispatchMouseEvent } from '../utils/dom'
import type { VisualCursor } from './visual-cursor'

/** 当前虚拟鼠标位置（全局状态） */
let currentPosition: Point = { x: 0, y: 0 }

/** 获取当前模拟鼠标位置 */
export function getCurrentMousePosition(): Point {
  return { ...currentPosition }
}

/** 更新当前模拟鼠标位置 */
export function setCurrentMousePosition(pos: Point): void {
  currentPosition = { ...pos }
}

/**
 * 鼠标模拟器
 * 通过贝塞尔曲线路径逐步触发 mousemove 事件，模拟真实鼠标移动行为
 * 可选绑定 VisualCursor 实现可视化效果
 */
export class MouseSimulator {
  private speed: number
  private debug: boolean
  /** 可视化光标实例（可选） */
  private visualCursor: VisualCursor | null = null

  constructor(options: MouseSimulatorOptions = {}) {
    this.speed = options.speed ?? 1.0
    this.debug = options.debug ?? false
  }

  /** 绑定可视化光标 */
  setVisualCursor(cursor: VisualCursor | null): void {
    this.visualCursor = cursor
  }

  /** 打印调试日志 */
  private log(...args: unknown[]): void {
    if (this.debug) {
      console.log('[MouseSimulator]', ...args)
    }
  }

  /**
   * 将鼠标平滑移动到指定坐标（视口坐标）
   * 同步更新 VisualCursor 位置
   */
  async moveTo(toX: number, toY: number, pathOptions?: BezierPathOptions): Promise<void> {
    const from: Point = { ...currentPosition }
    const to: Point = { x: toX, y: toY }

    this.log(`移动鼠标 (${from.x.toFixed(0)}, ${from.y.toFixed(0)}) → (${to.x.toFixed(0)}, ${to.y.toFixed(0)})`)

    // 生成贝塞尔曲线路径
    const path = generateMousePath(from, to, pathOptions)

    // 沿路径逐点移动
    for (let i = 1; i < path.length; i++) {
      const point = path[i]
      const prevPoint = path[i - 1]

      // 分发 mousemove 事件
      dispatchMouseEvent(document, 'mousemove', {
        clientX: Math.round(point.x),
        clientY: Math.round(point.y),
        screenX: Math.round(point.x),
        screenY: Math.round(point.y)
      })

      // 更新当前位置 + 可视化光标
      currentPosition = { x: Math.round(point.x), y: Math.round(point.y) }
      this.visualCursor?.setPosition(Math.round(point.x), Math.round(point.y))

      // 根据步进距离计算延迟
      const stepDuration = calculateStepDuration(prevPoint, point, this.speed)
      if (stepDuration > 2) {
        await sleep(stepDuration)
      }
    }

    currentPosition = { x: Math.round(to.x), y: Math.round(to.y) }
    this.visualCursor?.setPosition(Math.round(to.x), Math.round(to.y))
  }

  /**
   * 移动鼠标到指定元素的中心位置
   * 同时高亮目标元素
   */
  async moveToElement(selector: Selector, pathOptions?: BezierPathOptions): Promise<HTMLElement> {
    const el = findElement(selector)
    scrollIntoViewIfNeeded(el)
    await sleep(50)

    // 高亮目标元素
    this.visualCursor?.highlightElement(el)

    const center = getElementCenter(el)
    await this.moveTo(center.x, center.y, pathOptions)
    return el
  }

  /**
   * 模拟单击
   * 完整事件序列：mousemove → mouseenter → mouseover → mousedown → mouseup → click
   */
  async click(selector: Selector): Promise<void> {
    this.log(`单击 ${String(selector)}`)
    const el = await this.moveToElement(selector)
    const center = getElementCenter(el)

    const mouseOpts = {
      clientX: Math.round(center.x),
      clientY: Math.round(center.y),
      screenX: Math.round(center.x),
      screenY: Math.round(center.y)
    }

    // 鼠标进入元素
    dispatchMouseEvent(el, 'mouseenter', { ...mouseOpts, bubbles: false })
    dispatchMouseEvent(el, 'mouseover', mouseOpts)

    // 按下 + 可视化按下动画
    dispatchMouseEvent(el, 'mousedown', { ...mouseOpts, buttons: 1 })
    this.visualCursor?.animatePress()
    await sleep(50 + Math.random() * 70)

    // 抬起 + 点击 + 可视化释放动画（含波纹）
    dispatchMouseEvent(el, 'mouseup', { ...mouseOpts, buttons: 0 })
    dispatchMouseEvent(el, 'click', { ...mouseOpts, buttons: 0 })
    this.visualCursor?.animateRelease()

    // 延迟后隐藏高亮
    await sleep(200)
    this.visualCursor?.hideHighlight()

    this.log(`单击完成`)
  }

  /**
   * 模拟双击
   */
  async dblClick(selector: Selector): Promise<void> {
    this.log(`双击 ${String(selector)}`)
    const el = await this.moveToElement(selector)
    const center = getElementCenter(el)

    const mouseOpts = {
      clientX: Math.round(center.x),
      clientY: Math.round(center.y),
      screenX: Math.round(center.x),
      screenY: Math.round(center.y)
    }

    // 第一次点击
    dispatchMouseEvent(el, 'mousedown', { ...mouseOpts, buttons: 1 })
    this.visualCursor?.animatePress()
    await sleep(50 + Math.random() * 50)
    dispatchMouseEvent(el, 'mouseup', { ...mouseOpts, buttons: 0 })
    dispatchMouseEvent(el, 'click', mouseOpts)
    this.visualCursor?.animateRelease()

    await sleep(80 + Math.random() * 60)

    // 第二次点击
    dispatchMouseEvent(el, 'mousedown', { ...mouseOpts, buttons: 1 })
    this.visualCursor?.animatePress()
    await sleep(50 + Math.random() * 50)
    dispatchMouseEvent(el, 'mouseup', { ...mouseOpts, buttons: 0 })
    dispatchMouseEvent(el, 'click', mouseOpts)
    dispatchMouseEvent(el, 'dblclick', mouseOpts)
    this.visualCursor?.animateRelease()

    await sleep(200)
    this.visualCursor?.hideHighlight()

    this.log(`双击完成`)
  }

  /**
   * 模拟右键点击
   */
  async rightClick(selector: Selector): Promise<void> {
    this.log(`右键点击 ${String(selector)}`)
    const el = await this.moveToElement(selector)
    const center = getElementCenter(el)

    const mouseOpts = {
      clientX: Math.round(center.x),
      clientY: Math.round(center.y),
      screenX: Math.round(center.x),
      screenY: Math.round(center.y),
      button: 2,
      buttons: 2
    }

    dispatchMouseEvent(el, 'mousedown', mouseOpts)
    this.visualCursor?.animatePress()
    await sleep(50 + Math.random() * 50)
    dispatchMouseEvent(el, 'mouseup', mouseOpts)
    dispatchMouseEvent(el, 'contextmenu', mouseOpts)
    this.visualCursor?.animateRelease()

    await sleep(200)
    this.visualCursor?.hideHighlight()

    this.log(`右键点击完成`)
  }

  /**
   * 模拟悬停（移动到元素后等待指定时间）
   */
  async hover(selector: Selector, duration = 1000): Promise<void> {
    this.log(`悬停 ${String(selector)} ${duration}ms`)
    const el = await this.moveToElement(selector)
    const center = getElementCenter(el)

    const mouseOpts = {
      clientX: Math.round(center.x),
      clientY: Math.round(center.y),
      screenX: Math.round(center.x),
      screenY: Math.round(center.y)
    }

    dispatchMouseEvent(el, 'mouseenter', { ...mouseOpts, bubbles: false })
    dispatchMouseEvent(el, 'mouseover', mouseOpts)

    await sleep(duration)

    dispatchMouseEvent(el, 'mouseout', mouseOpts)
    dispatchMouseEvent(el, 'mouseleave', { ...mouseOpts, bubbles: false })

    this.visualCursor?.hideHighlight()

    this.log(`悬停结束`)
  }

  /**
   * 模拟拖拽
   */
  async drag(fromSelector: Selector, toSelector: Selector, duration = 600): Promise<void> {
    this.log(`拖拽 ${String(fromSelector)} → ${String(toSelector)}`)

    const fromEl = await this.moveToElement(fromSelector)
    const fromCenter = getElementCenter(fromEl)

    const fromOpts = {
      clientX: Math.round(fromCenter.x),
      clientY: Math.round(fromCenter.y),
      screenX: Math.round(fromCenter.x),
      screenY: Math.round(fromCenter.y),
      buttons: 1
    }

    dispatchMouseEvent(fromEl, 'mousedown', { ...fromOpts, button: 0 })
    dispatchMouseEvent(fromEl, 'dragstart', { ...fromOpts, button: 0 })
    this.visualCursor?.animatePress()
    await sleep(50)

    const toEl = findElement(toSelector)
    scrollIntoViewIfNeeded(toEl)
    const toCenter = getElementCenter(toEl)

    const from: Point = { x: fromCenter.x, y: fromCenter.y }
    const to: Point = { x: toCenter.x, y: toCenter.y }
    const path = generateMousePath(from, to, { points: 40, curvature: 0.2 })
    const stepDelay = duration / path.length

    for (const point of path) {
      dispatchMouseEvent(document, 'mousemove', {
        clientX: Math.round(point.x),
        clientY: Math.round(point.y),
        screenX: Math.round(point.x),
        screenY: Math.round(point.y),
        buttons: 1
      })
      dispatchMouseEvent(fromEl, 'drag', {
        clientX: Math.round(point.x),
        clientY: Math.round(point.y)
      })
      currentPosition = point
      this.visualCursor?.setPosition(Math.round(point.x), Math.round(point.y))
      await sleep(stepDelay)
    }

    const toOpts = {
      clientX: Math.round(toCenter.x),
      clientY: Math.round(toCenter.y),
      screenX: Math.round(toCenter.x),
      screenY: Math.round(toCenter.y)
    }

    dispatchMouseEvent(toEl, 'dragover', toOpts)
    dispatchMouseEvent(toEl, 'drop', toOpts)
    dispatchMouseEvent(document, 'mouseup', { ...toOpts, buttons: 0 })
    dispatchMouseEvent(fromEl, 'dragend', toOpts)
    this.visualCursor?.animateRelease()

    currentPosition = { x: Math.round(toCenter.x), y: Math.round(toCenter.y) }
    this.visualCursor?.hideHighlight()
    this.log(`拖拽完成`)
  }
}
