/**
 * UIAutomator 主引擎
 * @description 链式 API 构建动作队列，顺序执行，支持 human 化延迟、事件监听、暂停/恢复/停止
 */

import { InputSimulator } from './core/input-simulator'
import { MouseSimulator } from './core/mouse-simulator'
import { VisualCursor } from './core/visual-cursor'
import type { VisualCursorOptions } from './core/visual-cursor'
import type { AutomatorOptions, AutomatorEventType, EventListener, ActionEventData, Action, InputOptions, WaitForOptions, ScrollTarget, Selector, TooltipPlacement } from './types'
import { ActionType } from './types'
import { sleep, randomDelay, waitForElement, waitForCondition } from './utils/delay'
import { findElement, scrollIntoViewIfNeeded, focusElement, dispatchMouseEvent, getElementCenter, isVisible } from './utils/dom'

/** 引擎运行状态 */
type EngineState = 'idle' | 'running' | 'paused' | 'stopped'

/** 补齐默认值后的引擎配置（onError 保持可选） */
type ResolvedAutomatorOptions = Required<Omit<AutomatorOptions, 'onError'>> & Pick<AutomatorOptions, 'onError'>

/**
 * UIAutomator 主引擎
 *
 * @example
 * ```typescript
 * const bot = new UIAutomator({ humanize: true, debug: true });
 * await bot
 *   .click('#login-btn')
 *   .wait(500)
 *   .input('#username', 'admin', { speed: 80, clear: true })
 *   .waitFor('#dashboard')
 *   .run();
 * ```
 */
export class UIAutomator {
  // ---- 配置 ----
  private options: ResolvedAutomatorOptions

  // ---- 内部模块 ----
  private mouse: MouseSimulator
  private keyboard: InputSimulator
  private visualCursor: VisualCursor

  // ---- 动作队列 ----
  private queue: Action[] = []

  // ---- 执行状态 ----
  private state: EngineState = 'idle'
  /** 暂停时 resolve 当前 pending Promise */
  private resumeResolver: (() => void) | null = null

  // ---- 事件系统 ----
  private listeners: Map<AutomatorEventType, EventListener<unknown>[]> = new Map()

  constructor(options: AutomatorOptions & { cursor?: VisualCursorOptions } = {}) {
    this.options = {
      humanize: options.humanize ?? true,
      humanizeRange: options.humanizeRange ?? [60, 180],
      mouseSpeed: options.mouseSpeed ?? 1.0,
      inputSpeed: options.inputSpeed ?? 80,
      debug: options.debug ?? false,
      maxRetries: options.maxRetries ?? 10,
      retryInterval: options.retryInterval ?? 2000,
      onError: options.onError
    }

    // 创建可视化光标
    this.visualCursor = new VisualCursor(options.cursor ?? { enabled: true })
    VisualCursor.injectStyles()

    this.mouse = new MouseSimulator({
      speed: this.options.mouseSpeed,
      debug: this.options.debug
    })

    // 绑定可视化光标到鼠标模拟器
    this.mouse.setVisualCursor(this.visualCursor)

    this.keyboard = new InputSimulator({
      speed: this.options.inputSpeed,
      debug: this.options.debug
    })
  }

  // ============================================================
  // 调试日志
  // ============================================================

  private log(...args: unknown[]): void {
    if (this.options.debug) {
      console.log('[UIAutomator]', ...args)
    }
  }

  // ============================================================
  // 事件系统
  // ============================================================

  /** 注册事件监听器 */
  on<T = unknown>(event: AutomatorEventType, listener: EventListener<T>): this {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, [])
    }
    this.listeners.get(event)!.push(listener as EventListener<unknown>)
    return this
  }

  /** 移除事件监听器 */
  off<T = unknown>(event: AutomatorEventType, listener: EventListener<T>): this {
    const list = this.listeners.get(event)
    if (list) {
      const idx = list.indexOf(listener as EventListener<unknown>)
      if (idx !== -1) list.splice(idx, 1)
    }
    return this
  }

  /** 触发事件(逐 listener 隔离异常:单个监听器抛错只记录日志,不炸穿调用方、不中断其余监听器) */
  private emit<T>(event: AutomatorEventType, data?: T): void {
    this.listeners.get(event)?.forEach(fn => {
      try {
        fn(data)
      } catch (err) {
        this.log(`事件 ${event} 监听器抛错:`, err instanceof Error ? err.message : err)
      }
    })
  }

  // ============================================================
  // 链式 API - 动作入队
  // ============================================================

  /**
   * 单击元素
   * @param selector CSS 选择器、XPath 或 Element
   * @param delay 动作前额外延迟 ms
   */
  click(selector: Selector, delay?: number): this {
    this.queue.push({ type: ActionType.CLICK, selector, delay })
    return this
  }

  /**
   * 双击元素
   */
  dblClick(selector: Selector): this {
    this.queue.push({ type: ActionType.DBL_CLICK, selector })
    return this
  }

  /**
   * 右键点击元素
   */
  rightClick(selector: Selector): this {
    this.queue.push({ type: ActionType.RIGHT_CLICK, selector })
    return this
  }

  /**
   * 悬停在元素上
   * @param selector 目标元素
   * @param duration 悬停时间 ms（默认 1000）
   */
  hover(selector: Selector, duration?: number): this {
    this.queue.push({ type: ActionType.HOVER, selector, duration })
    return this
  }

  /**
   * 悬停在元素上并显示 tooltip 文字气泡
   * @param selector 目标元素
   * @param text tooltip 显示文字
   * @param duration 悬停及 tooltip 显示时间 ms（默认 2000）
   * @param placement tooltip 弹出方向（默认 'top'）
   */
  tooltip(selector: Selector, text: string, duration?: number, placement?: TooltipPlacement): this {
    this.queue.push({ type: ActionType.TOOLTIP, selector, text, duration, placement })
    return this
  }

  /**
   * 聚焦元素
   * @param selector 目标元素
   */
  focus(selector: Selector): this {
    this.queue.push({ type: ActionType.FOCUS, selector })
    return this
  }

  /**
   * 输入文本（默认逐字符打字）
   * @param selector 目标输入元素
   * @param text 要输入的文本
   * @param options 输入选项（speed、clear、typeMode）
   */
  input(selector: Selector, text: string, options?: InputOptions): this {
    this.queue.push({ type: ActionType.INPUT, selector, text, options })
    return this
  }

  /**
   * 按下特殊键（如 'Enter'/'Escape'/'Tab'/'Backspace'；普通文本输入用 input）
   * @param selector 目标元素
   * @param key 特殊键名（见 InputSimulator 的 SPECIAL_KEYS 表）
   */
  pressKey(selector: Selector, key: string): this {
    this.queue.push({ type: ActionType.PRESS_KEY, selector, key })
    return this
  }

  /**
   * 滚动元素
   * @param selector 要滚动的元素
   * @param target 滚动目标：'top' | 'center' | 'bottom' | 'nearest' | 数值(px)
   * @param container 滚动容器（不传则以元素自身或 window 为容器）
   */
  scroll(selector: Selector, target: ScrollTarget, container?: Selector): this {
    this.queue.push({ type: ActionType.SCROLL, selector, target, container })
    return this
  }

  /**
   * 拖拽元素
   * @param fromSelector 起始元素
   * @param toSelector 目标元素
   * @param duration 拖拽持续时间 ms（默认 600）
   */
  drag(fromSelector: Selector, toSelector: Selector, duration?: number): this {
    this.queue.push({ type: ActionType.DRAG, fromSelector, toSelector, duration })
    return this
  }

  /**
   * 等待固定时间
   * @param duration 等待毫秒数
   */
  wait(duration: number): this {
    this.queue.push({ type: ActionType.WAIT, duration })
    return this
  }

  /**
   * 等待元素出现/消失，或等待自定义条件
   * @param selector CSS/XPath 字符串、Element，或返回 boolean 的函数
   * @param options 超时、轮询间隔、可见性选项
   */
  waitFor(selector: Selector | (() => boolean | Promise<boolean>), options?: WaitForOptions): this {
    this.queue.push({ type: ActionType.WAIT_FOR, selector, options })
    return this
  }

  /**
   * 在动作队列中插入一个自定义异步函数
   * 适用于路由跳转、API 调用、状态修改等任何自定义操作
   *
   * @param fn 要执行的函数（支持 async）
   * @param label 调试日志标签（可选）
   *
   * @example
   * ```typescript
   * bot
   *   .click('#login-btn')
   *   .exec(async () => {
   *     await router.push('/dashboard')
   *   }, '跳转到仪表盘')
   *   .waitFor('#dashboard-content')
   *   .run()
   * ```
   */
  exec(fn: () => void | Promise<void>, label?: string): this {
    this.queue.push({ type: ActionType.RUN_FN, fn, label })
    return this
  }

  // ============================================================
  // 执行控制
  // ============================================================

  /**
   * 暂停执行（当前动作完成后暂停）
   */
  pause(): void {
    if (this.state === 'running') {
      this.state = 'paused'
      this.emit('pause')
      this.log('已暂停')
    }
  }

  /**
   * 恢复执行
   */
  resume(): void {
    if (this.state === 'paused') {
      this.state = 'running'
      this.emit('resume')
      this.resumeResolver?.()
      this.resumeResolver = null
      this.log('已恢复')
    }
  }

  /**
   * 停止执行并清空队列
   */
  stop(): void {
    this.state = 'stopped'
    this.queue = []
    this.resumeResolver?.()
    this.resumeResolver = null
    this.emit('stop')
    this.log('已停止')
  }

  /**
   * 重置引擎状态（清空队列、重置为 idle）
   */
  reset(): this {
    this.state = 'idle'
    this.queue = []
    this.resumeResolver = null
    this.log('已重置')
    return this
  }

  /** 获取当前引擎状态 */
  getState(): EngineState {
    return this.state
  }

  /** 获取当前队列长度 */
  getQueueLength(): number {
    return this.queue.length
  }

  // ============================================================
  // 核心执行逻辑
  // ============================================================

  /** 等待 resume 信号（用于 pause 状态） */
  private waitForResume(): Promise<void> {
    return new Promise(resolve => {
      this.resumeResolver = resolve
    })
  }

  /**
   * 统一清理可视化光标
   * 在任务完成、停止或发生错误时调用，确保模拟鼠标图标被移除
   */
  private async cleanupCursor(): Promise<void> {
    this.visualCursor.hide()
    await sleep(300)
    this.visualCursor.unmount()
  }

  /**
   * 执行单个动作
   */
  private async executeAction(action: Action): Promise<void> {
    switch (action.type) {
      case ActionType.CLICK:
        if (action.delay) await sleep(action.delay)
        await this.mouse.click(action.selector)
        break

      case ActionType.DBL_CLICK:
        await this.mouse.dblClick(action.selector)
        break

      case ActionType.RIGHT_CLICK:
        await this.mouse.rightClick(action.selector)
        break

      case ActionType.HOVER:
        await this.mouse.hover(action.selector, action.duration ?? 1000)
        break

      case ActionType.TOOLTIP:
        await this.executeTooltip(action.selector, action.text, action.duration ?? 2000, action.placement ?? 'top')
        break

      case ActionType.FOCUS:
        focusElement(findElement(action.selector))
        await sleep(50)
        break

      case ActionType.INPUT:
        await this.keyboard.type(action.selector, action.text, action.options)
        break

      case ActionType.PRESS_KEY:
        await this.keyboard.pressKey(action.selector, action.key)
        break

      case ActionType.SCROLL:
        await this.executeScroll(action.selector, action.target, action.container)
        break

      case ActionType.DRAG:
        await this.mouse.drag(action.fromSelector, action.toSelector, action.duration ?? 600)
        break

      case ActionType.WAIT:
        await sleep(action.duration)
        break

      case ActionType.WAIT_FOR: {
        const { selector, options } = action
        if (typeof selector === 'function') {
          await waitForCondition(selector, options)
        } else if (typeof selector === 'string') {
          await waitForElement(selector, options)
        } else {
          // 等待 Element/HTMLElement 可见或消失
          const el = selector as Element
          await waitForCondition(() => {
            const visible = options?.visible ?? true
            return visible ? isVisible(el) : !isVisible(el)
          }, options)
        }
        break
      }

      case ActionType.RUN_FN:
        this.log(`执行自定义函数${action.label ? ` [${action.label}]` : ''}`)
        await action.fn()
        break

      default:
        this.log('未知动作类型，跳过')
    }
  }

  /**
   * 执行 tooltip 动作：移动鼠标到元素，同步显示文字气泡，duration 同时作用于悬停和 tooltip 显示
   */
  private async executeTooltip(selector: Selector, text: string, duration: number, placement: TooltipPlacement): Promise<void> {
    // 移动鼠标到目标元素（带动画轨迹）
    const el = await this.mouse.moveToElement(selector)
    const center = getElementCenter(el)
    const mouseOpts = {
      clientX: Math.round(center.x),
      clientY: Math.round(center.y),
      screenX: Math.round(center.x),
      screenY: Math.round(center.y)
    }

    // 触发 hover 相关事件（与原生 hover 行为一致）
    dispatchMouseEvent(el, 'mouseenter', { ...mouseOpts, bubbles: false })
    dispatchMouseEvent(el, 'mouseover', mouseOpts)

    // 取元素当前位置（moveToElement 可能触发 scrollIntoView，位置已稳定）
    const rect = el.getBoundingClientRect()

    // 取主题色（与光标、高亮、波纹保持一致）
    const themeColor = this.visualCursor.getColor()

    // ---- 创建 tooltip DOM ----
    const tip = document.createElement('div')
    tip.setAttribute('data-ui-automator-tooltip', '')
    Object.assign(tip.style, {
      position: 'fixed',
      zIndex: '2147483647',
      padding: '6px 12px',
      background: themeColor,
      color: '#fff',
      fontSize: '12px',
      lineHeight: '1.6',
      borderRadius: '6px',
      border: `1px solid ${themeColor}`,
      boxShadow: `0 4px 16px ${themeColor}4d`,
      pointerEvents: 'none',
      whiteSpace: 'nowrap',
      opacity: '0',
      transition: 'opacity 0.18s ease'
    })
    tip.textContent = text

    // 小箭头（颜色与背景匹配）
    const arrow = document.createElement('div')
    const arrowColor = themeColor
    Object.assign(arrow.style, {
      position: 'absolute',
      width: '0',
      height: '0',
      pointerEvents: 'none'
    })
    tip.appendChild(arrow)
    document.body.appendChild(tip)

    // 渲染后取气泡实际尺寸
    const GAP = 10
    const tw = tip.offsetWidth
    const th = tip.offsetHeight
    const arrowSize = 6

    let tipTop = 0
    let tipLeft = 0

    if (placement === 'top') {
      tipTop = rect.top - th - GAP
      tipLeft = rect.left + rect.width / 2 - tw / 2
      Object.assign(arrow.style, {
        bottom: `-${arrowSize}px`,
        left: '50%',
        transform: 'translateX(-50%)',
        borderLeft: `${arrowSize}px solid transparent`,
        borderRight: `${arrowSize}px solid transparent`,
        borderTop: `${arrowSize}px solid ${arrowColor}`
      })
    } else if (placement === 'bottom') {
      tipTop = rect.bottom + GAP
      tipLeft = rect.left + rect.width / 2 - tw / 2
      Object.assign(arrow.style, {
        top: `-${arrowSize}px`,
        left: '50%',
        transform: 'translateX(-50%)',
        borderLeft: `${arrowSize}px solid transparent`,
        borderRight: `${arrowSize}px solid transparent`,
        borderBottom: `${arrowSize}px solid ${arrowColor}`
      })
    } else if (placement === 'left') {
      tipTop = rect.top + rect.height / 2 - th / 2
      tipLeft = rect.left - tw - GAP
      Object.assign(arrow.style, {
        right: `-${arrowSize}px`,
        top: '50%',
        transform: 'translateY(-50%)',
        borderTop: `${arrowSize}px solid transparent`,
        borderBottom: `${arrowSize}px solid transparent`,
        borderLeft: `${arrowSize}px solid ${arrowColor}`
      })
    } else {
      tipTop = rect.top + rect.height / 2 - th / 2
      tipLeft = rect.right + GAP
      Object.assign(arrow.style, {
        left: `-${arrowSize}px`,
        top: '50%',
        transform: 'translateY(-50%)',
        borderTop: `${arrowSize}px solid transparent`,
        borderBottom: `${arrowSize}px solid transparent`,
        borderRight: `${arrowSize}px solid ${arrowColor}`
      })
    }

    // 视口边界修正
    const vw = window.innerWidth
    const vh = window.innerHeight
    tipLeft = Math.max(GAP, Math.min(tipLeft, vw - tw - GAP))
    tipTop = Math.max(GAP, Math.min(tipTop, vh - th - GAP))

    tip.style.left = `${tipLeft}px`
    tip.style.top = `${tipTop}px`

    // 淡入
    requestAnimationFrame(() => {
      tip.style.opacity = '1'
    })

    // 鼠标停留 + tooltip 显示，共享同一个 duration
    await sleep(duration)

    // 淡出 tooltip
    tip.style.opacity = '0'

    // 同步结束 hover 状态
    dispatchMouseEvent(el, 'mouseout', mouseOpts)
    dispatchMouseEvent(el, 'mouseleave', { ...mouseOpts, bubbles: false })
    this.visualCursor.hideHighlight()

    await sleep(200)
    tip.remove()
  }

  /**
   * 执行滚动动作
   */
  private async executeScroll(selector: Selector, target: ScrollTarget, containerSelector?: Selector): Promise<void> {
    const el = findElement(selector)
    scrollIntoViewIfNeeded(el)
    await sleep(50)

    // 确定滚动容器
    const container = containerSelector ? findElement(containerSelector) : el

    let scrollTop: number
    if (target === 'top') {
      scrollTop = 0
    } else if (target === 'bottom') {
      scrollTop = container.scrollHeight - container.clientHeight
    } else if (target === 'center') {
      scrollTop = (container.scrollHeight - container.clientHeight) / 2
    } else if (target === 'nearest') {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      return
    } else {
      // 数值：相对滚动
      scrollTop = container.scrollTop + (target as number)
    }

    container.scrollTo({ top: scrollTop, behavior: 'smooth' })
    // 等待滚动完成
    await sleep(400)
  }

  /**
   * 执行已入队的所有动作
   */
  async run(): Promise<void> {
    if (this.state === 'running') {
      this.log('引擎已在运行中')
      return
    }

    this.state = 'running'
    const actions = [...this.queue]
    this.queue = []
    const total = actions.length

    // 挂载并显示可视化光标
    this.visualCursor.mount()
    this.visualCursor.show()

    this.log(`开始执行，共 ${total} 个动作`)

    try {
      for (let i = 0; i < actions.length; i++) {
        // 检查是否已停止
        if (this.getState() === 'stopped') {
          this.log('执行已停止')
          await this.cleanupCursor()
          return
        }

        // 检查是否暂停
        if (this.getState() === 'paused') {
          this.log(`第 ${i + 1} 个动作执行前暂停，等待 resume...`)
          await this.waitForResume()
          if (this.getState() === 'stopped') return
        }

        const action = actions[i]
        const eventData: ActionEventData = { action, index: i, total }

        this.log(`执行动作 ${i + 1}/${total}: ${action.type}`)
        this.emit('action:start', eventData)

        let success = false
        let retryCount = 0
        let lastError: Error | null = null

        while (!success && retryCount <= this.options.maxRetries) {
          // 检查是否已停止 (如果在重试等待中被强制停止)
          if (this.getState() === 'stopped') {
            this.log('执行已停止 (重试中断)')
            await this.cleanupCursor()
            return
          }

          try {
            await this.executeAction(action)
            this.emit('action:end', eventData)
            success = true
          } catch (err) {
            lastError = err instanceof Error ? err : new Error(String(err))

            if (retryCount < this.options.maxRetries) {
              this.log(`动作 ${action.type} 执行失败 (尝试 ${retryCount + 1}/${this.options.maxRetries}):`, lastError.message)
              this.log(`等待 ${this.options.retryInterval}ms 后进行下一次重试...`)
              retryCount++
              await sleep(this.options.retryInterval)
            } else {
              // 超过最大重试次数
              break
            }
          }
        }

        if (!success && lastError) {
          this.log(`动作 ${action.type} 最终执行失败 (已重试 ${this.options.maxRetries} 次):`, lastError.message)
          this.emit('action:error', { ...eventData, error: lastError })

          // 无论是否有自定义错误处理器，都先清理光标
          await this.cleanupCursor()

          if (this.options.onError) {
            this.options.onError(lastError, action)
          } else {
            this.state = 'idle'
            this.emit('error', lastError)
            throw lastError
          }
        }

        // 人性化延迟（最后一个动作后不加）
        if (this.options.humanize && i < actions.length - 1) {
          await randomDelay(this.options.humanizeRange)
        }
      }

      this.state = 'idle'

      // 任务正常完成，清理可视化光标
      await this.cleanupCursor()

      this.log('所有动作执行完成')
      this.emit('complete')
    } finally {
      // 异常穿透兜底:正常/停止/错误路径均已自行设置终态并清理;
      // 仍为 running 说明异常绕过了上面各分支,复位并清理光标,避免引擎永久卡在 running
      if (this.state === 'running') {
        this.state = 'idle'
        await this.cleanupCursor()
      }
    }
  }
}
