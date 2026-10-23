/**
 * UIAutomator - UI 自动化执行工具库
 * @description 零依赖、纯 TypeScript 实现的浏览器 UI 自动化工具库
 * 支持模拟真实鼠标移动（贝塞尔曲线）、逐字符键盘输入、延迟等待等功能
 *
 * @example 链式 API
 * ```typescript
 * import { UIAutomator } from '@repo/ui-automator';
 *
 * const bot = new UIAutomator({ humanize: true, debug: true });
 * await bot
 *   .click('#login-btn')
 *   .wait(500)
 *   .input('#username', 'admin@example.com', { clear: true })
 *   .input('#password', '123456')
 *   .click('#submit')
 *   .waitFor('#dashboard', { timeout: 5000 })
 *   .run();
 * ```
 *
 * @example 独立工具函数
 * ```typescript
 * import { sleep, randomDelay, waitForElement } from '@repo/ui-automator';
 *
 * await sleep(500);
 * await randomDelay([100, 300]);
 * await waitForElement('#toast', { timeout: 3000 });
 * ```
 */

// ---- 主引擎 ----
export { UIAutomator } from './engine'

// ---- 核心模拟器（独立使用）----
export { MouseSimulator, getCurrentMousePosition, setCurrentMousePosition } from './core/mouse-simulator'
export { InputSimulator } from './core/input-simulator'
export { VisualCursor, resolveThemeColor } from './core/visual-cursor'
export type { VisualCursorOptions } from './core/visual-cursor'

// ---- 工具函数（独立使用）----
export { sleep, randomDelay, addNoise, waitForElement, waitForCondition } from './utils/delay'

export {
  queryElementByString,
  findElement,
  tryFindElement,
  isVisible,
  isInViewport,
  getElementRect,
  getElementCenter,
  scrollIntoViewIfNeeded,
  dispatchMouseEvent,
  dispatchKeyEvent,
  dispatchInputEvent,
  dispatchChangeEvent,
  focusElement,
  blurElement,
  getNativeInputSetter
} from './utils/dom'

export { generateMousePath, calculateStepDuration } from './utils/bezier'

// ---- 类型导出 ----
export type {
  AutomatorOptions,
  AutomatorEventType,
  EventListener,
  ActionEventData,
  Action,
  ClickAction,
  HoverAction,
  TooltipAction,
  TooltipPlacement,
  FocusAction,
  InputAction,
  InputOptions,
  ScrollAction,
  ScrollTarget,
  DragAction,
  WaitAction,
  WaitForAction,
  WaitForOptions,
  RunFnAction,
  MouseSimulatorOptions,
  InputSimulatorOptions,
  BezierPathOptions,
  Point,
  ElementRect,
  Selector
} from './types'

// ActionType 枚举是值，需要单独 export（不用 export type）
export { ActionType } from './types'
