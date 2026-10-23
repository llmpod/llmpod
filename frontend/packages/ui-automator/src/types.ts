/**
 * UI 自动化工具库 - 类型定义
 * @description 所有接口、枚举和类型定义
 */

// ============================================================
// 坐标与矩形
// ============================================================

/** 二维坐标点 */
export interface Point {
  x: number
  y: number
}

/** 元素矩形信息 */
export interface ElementRect {
  x: number
  y: number
  width: number
  height: number
  /** 中心点 X */
  centerX: number
  /** 中心点 Y */
  centerY: number
}

// ============================================================
// 选择器
// ============================================================

/** 选择器（字符串 CSS 或 XPath、或直接传 Element） */
export type Selector = string | Element | HTMLElement

// ============================================================
// 动作类型
// ============================================================

/** 动作类型枚举 */
export enum ActionType {
  CLICK = 'click',
  DBL_CLICK = 'dblClick',
  RIGHT_CLICK = 'rightClick',
  HOVER = 'hover',
  TOOLTIP = 'tooltip',
  FOCUS = 'focus',
  INPUT = 'input',
  PRESS_KEY = 'pressKey',
  SCROLL = 'scroll',
  DRAG = 'drag',
  WAIT = 'wait',
  WAIT_FOR = 'waitFor',
  RUN_FN = 'runFn'
}

/** 点击动作 */
export interface ClickAction {
  type: ActionType.CLICK | ActionType.DBL_CLICK | ActionType.RIGHT_CLICK
  selector: Selector
  /** 动作前额外延迟 ms */
  delay?: number
}

/** 悬停动作 */
export interface HoverAction {
  type: ActionType.HOVER
  selector: Selector
  /** 悬停持续时间 ms（默认 1000）*/
  duration?: number
}

/** tooltip 弹出位置 */
export type TooltipPlacement = 'top' | 'bottom' | 'left' | 'right'

/** tooltip 动作 */
export interface TooltipAction {
  type: ActionType.TOOLTIP
  selector: Selector
  /** 要显示的文字 */
  text: string
  /** 悬停及 tooltip 显示时间 ms（默认 2000）*/
  duration?: number
  /** tooltip 弹出方向（默认 'top'）*/
  placement?: TooltipPlacement
}

/** 聚焦动作 */
export interface FocusAction {
  type: ActionType.FOCUS
  selector: Selector
}

/** 输入动作选项 */
export interface InputOptions {
  /** 打字速度 ms/字符（默认继承引擎 inputSpeed）*/
  speed?: number
  /** 输入前是否清空（默认 false）*/
  clear?: boolean
  /** 是否逐字符打字（默认 true，false 则直接 setValue）*/
  typeMode?: boolean
}

/** 输入动作 */
export interface InputAction {
  type: ActionType.INPUT
  selector: Selector
  text: string
  options?: InputOptions
}

/** 按键动作(特殊键,如 Enter/Escape/Tab;普通文本输入用 InputAction) */
export interface PressKeyAction {
  type: ActionType.PRESS_KEY
  selector: Selector
  /** 特殊键名(见 InputSimulator 的 SPECIAL_KEYS 表,如 'Enter'/'Escape'/'Tab') */
  key: string
}

/** 滚动目标 */
export type ScrollTarget = 'top' | 'center' | 'bottom' | 'nearest' | number

/** 滚动动作 */
export interface ScrollAction {
  type: ActionType.SCROLL
  selector: Selector
  target: ScrollTarget
  /** 滚动容器选择器（默认为 selector 元素本身或 window）*/
  container?: Selector
}

/** 拖拽动作 */
export interface DragAction {
  type: ActionType.DRAG
  fromSelector: Selector
  toSelector: Selector
  /** 拖拽持续时间 ms（默认 600）*/
  duration?: number
}

/** 等待固定时间动作 */
export interface WaitAction {
  type: ActionType.WAIT
  duration: number
}

/** 等待元素选项 */
export interface WaitForOptions {
  /** 超时时间 ms（默认 10000）*/
  timeout?: number
  /** 轮询间隔 ms（默认 100）*/
  interval?: number
  /** 是否等待可见（false 表示等待消失，默认 true）*/
  visible?: boolean
}

/** 等待元素/条件动作 */
export interface WaitForAction {
  type: ActionType.WAIT_FOR
  /** 当 selector 为 string/Element 时等待该元素；为 function 时等待函数返回 true */
  selector: Selector | (() => boolean | Promise<boolean>)
  options?: WaitForOptions
}

/** 执行自定义函数动作 */
export interface RunFnAction {
  type: ActionType.RUN_FN
  fn: () => void | Promise<void>
  label?: string
}

/** 所有动作的联合类型 */
export type Action = ClickAction | HoverAction | TooltipAction | FocusAction | InputAction | PressKeyAction | ScrollAction | DragAction | WaitAction | WaitForAction | RunFnAction

// ============================================================
// 引擎配置
// ============================================================

/** UIAutomator 引擎配置 */
export interface AutomatorOptions {
  /**
   * 是否启用人性化随机延迟
   * @default true
   */
  humanize?: boolean
  /**
   * 动作间随机延迟范围 [min, max] ms
   * @default [60, 180]
   */
  humanizeRange?: [number, number]
  /**
   * 鼠标移动速度系数（>1 更快，<1 更慢）
   * @default 1.0
   */
  mouseSpeed?: number
  /**
   * 打字速度 ms/字符
   * @default 80
   */
  inputSpeed?: number
  /**
   * 调试日志
   * @default false
   */
  debug?: boolean
  /**
   * 动作执行失败时的最大重试次数
   * @default 10
   */
  maxRetries?: number
  /**
   * 动作执行失败时的重试间隔 ms
   * @default 2000
   */
  retryInterval?: number
  /**
   * 全局错误处理（不设置则抛出错误）
   */
  onError?: (err: Error, action?: Action) => void
}

// ============================================================
// 鼠标模拟器配置
// ============================================================

/** 鼠标模拟器配置 */
export interface MouseSimulatorOptions {
  /** 速度系数（默认 1.0）*/
  speed?: number
  /** 调试日志 */
  debug?: boolean
}

/** 贝塞尔曲线路径生成选项 */
export interface BezierPathOptions {
  /** 路径点数量（默认 50）*/
  points?: number
  /** 弯曲度 0-1（默认 0.3）*/
  curvature?: number
  /** 随机噪声强度 0-1（默认 0.1）*/
  noise?: number
}

// ============================================================
// 输入模拟器配置
// ============================================================

/** 输入模拟器配置 */
export interface InputSimulatorOptions {
  /** 打字速度 ms/字符（默认 80）*/
  speed?: number
  /** 调试日志 */
  debug?: boolean
}

// ============================================================
// 事件系统
// ============================================================

/** 引擎事件类型 */
export type AutomatorEventType = 'action:start' | 'action:end' | 'action:error' | 'pause' | 'resume' | 'stop' | 'complete' | 'error'

/** 事件监听器 */
export type EventListener<T = unknown> = (data: T) => void

/** 动作事件数据 */
export interface ActionEventData {
  action: Action
  index: number
  total: number
}
