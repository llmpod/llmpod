/**
 * 可视化光标模块
 * @description 在页面上渲染一个虚拟鼠标光标，跟随自动化操作移动，并提供点击波纹、目标高亮等视觉反馈
 */

/** 可视化光标配置 */
export interface VisualCursorOptions {
  /** 是否启用（默认 true） */
  enabled?: boolean
  /** 光标颜色（默认 'auto' 自动读取主题色 CSS 变量） */
  color?: string
  /** 光标大小 px（默认 20） */
  size?: number
  /** 是否显示点击波纹（默认 true） */
  showRipple?: boolean
  /** 是否显示目标高亮（默认 true） */
  showHighlight?: boolean
  /** 光标 z-index（默认 999999） */
  zIndex?: number
}

const DEFAULT_OPTIONS: Required<VisualCursorOptions> = {
  enabled: true,
  color: 'auto',
  size: 20,
  showRipple: true,
  showHighlight: true,
  zIndex: 999999
}

/** 默认主题色（Naive UI 默认主色） */
const FALLBACK_COLOR = '#18a058'

/**
 * 从 CSS 变量中解析主题色，按优先级依次尝试：
 * 1. --c-primary（本仓库主题系统注入的完整色值）
 * 2. --n-primary（本仓库主题系统注入的 RGB 逗号串，转为 rgb()）
 * 3. --el-color-primary（兼容 Element Plus 项目）
 * 读取不到则使用 Naive UI 默认主色
 */
export function resolveThemeColor(color: string): string {
  if (color !== 'auto') return color
  const rootStyle = getComputedStyle(document.documentElement)

  const fullColor = rootStyle.getPropertyValue('--c-primary').trim()
  if (fullColor) return fullColor

  const rgbTriplet = rootStyle.getPropertyValue('--n-primary').trim()
  if (rgbTriplet) return `rgb(${rgbTriplet})`

  const elPrimary = rootStyle.getPropertyValue('--el-color-primary').trim()
  return elPrimary || FALLBACK_COLOR
}

/**
 * 可视化光标管理器
 * 在自动化操作时显示一个虚拟光标，跟随鼠标移动路径
 * 位置更新统一走 transform（translate3d），避免 left/top 引发布局抖动
 */
export class VisualCursor {
  private options: Required<VisualCursorOptions>

  private cursorEl: HTMLElement | null = null

  private rippleContainer: HTMLElement | null = null

  private highlightEl: HTMLElement | null = null

  private mounted = false

  /** 运行时解析后的实际颜色值 */
  private resolvedColor = FALLBACK_COLOR

  /** 光标当前位置（视口坐标） */
  private posX = -100
  private posY = -100

  /** 光标当前缩放（按下/隐藏动画使用） */
  private scale = 1

  constructor(options: VisualCursorOptions = {}) {
    this.options = { ...DEFAULT_OPTIONS, ...options }
  }

  // ============================================================
  // 生命周期
  // ============================================================

  /** 挂载光标到 DOM（自动创建元素） */
  mount(): void {
    if (this.mounted || !this.options.enabled) return

    // 运行时解析主题色（读取 CSS 变量）
    this.resolvedColor = resolveThemeColor(this.options.color)

    // 创建光标元素
    this.cursorEl = this.createCursorElement()
    document.body.appendChild(this.cursorEl)

    // 创建波纹容器
    this.rippleContainer = document.createElement('div')
    this.rippleContainer.id = 'ui-automator-ripple-container'
    this.rippleContainer.style.cssText = `position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:${this.options.zIndex + 1};`
    document.body.appendChild(this.rippleContainer)

    // 创建目标高亮元素
    if (this.options.showHighlight) {
      this.highlightEl = this.createHighlightElement()
      document.body.appendChild(this.highlightEl)
    }

    this.mounted = true
  }

  /** 卸载光标，移除所有创建的 DOM 元素 */
  unmount(): void {
    if (!this.mounted) return

    this.cursorEl?.remove()
    this.rippleContainer?.remove()
    this.highlightEl?.remove()
    this.cursorEl = null
    this.rippleContainer = null
    this.highlightEl = null
    this.mounted = false
  }

  /** 显示光标 */
  show(): void {
    if (!this.cursorEl) return
    this.scale = 1
    this.cursorEl.style.opacity = '1'
    this.render()
  }

  /** 获取当前解析后的主题色（供外部使用） */
  getColor(): string {
    return this.resolvedColor
  }

  /** 隐藏光标 */
  hide(): void {
    if (!this.cursorEl) return
    this.scale = 0.5
    this.cursorEl.style.opacity = '0'
    this.render()
  }

  // ============================================================
  // 光标移动
  // ============================================================

  /** 将内部位置/缩放状态写入 transform */
  private render(): void {
    if (!this.cursorEl) return
    this.cursorEl.style.transform = `translate3d(${this.posX}px, ${this.posY}px, 0) scale(${this.scale})`
  }

  /**
   * 将光标立即移动到指定位置（无动画）
   */
  setPosition(x: number, y: number): void {
    this.posX = x
    this.posY = y
    this.render()
  }

  /**
   * 将光标平滑移动到指定位置（CSS transition）
   * @param x 目标 X（视口坐标）
   * @param y 目标 Y（视口坐标）
   * @param durationMs 过渡时间 ms
   */
  async smoothMoveTo(x: number, y: number, durationMs = 300): Promise<void> {
    if (!this.cursorEl) return

    this.cursorEl.style.transition = `transform ${durationMs}ms cubic-bezier(0.33, 1, 0.68, 1), opacity 0.2s ease`
    this.setPosition(x, y)

    await new Promise<void>(resolve => setTimeout(resolve, durationMs))
    // 移动结束后移除 transition 方便逐帧更新
    this.cursorEl.style.transition = 'none'
  }

  // ============================================================
  // 点击动画
  // ============================================================

  /**
   * 播放点击按下动画（光标缩小）
   */
  animatePress(): void {
    if (!this.cursorEl) return
    this.cursorEl.style.transition = 'transform 0.1s ease'
    this.scale = 0.75
    this.render()
  }

  /**
   * 播放点击释放动画（光标恢复 + 波纹效果）
   */
  animateRelease(): void {
    if (!this.cursorEl) return
    this.cursorEl.style.transition = 'transform 0.15s ease'
    this.scale = 1
    this.render()

    // 播放波纹
    if (this.options.showRipple) {
      this.createRipple(this.posX, this.posY)
    }
  }

  /**
   * 在指定位置创建点击波纹效果
   */
  private createRipple(x: number, y: number): void {
    if (!this.rippleContainer) return

    const ripple = document.createElement('div')
    const size = 40
    ripple.style.cssText = `
      position: fixed;
      left: ${x - size / 2}px;
      top: ${y - size / 2}px;
      width: ${size}px;
      height: ${size}px;
      border-radius: 50%;
      background: ${this.resolvedColor};
      opacity: 0.4;
      pointer-events: none;
      animation: ui-automator-ripple 0.6s ease-out forwards;
    `
    this.rippleContainer.appendChild(ripple)

    // 动画结束后移除
    setTimeout(() => ripple.remove(), 650)
  }

  // ============================================================
  // 目标元素高亮
  // ============================================================

  /**
   * 高亮指定元素（半透明边框 + 背景）
   */
  highlightElement(el: Element): void {
    if (!this.highlightEl || !this.options.showHighlight) return

    const rect = el.getBoundingClientRect()
    const padding = 4

    this.highlightEl.style.left = `${rect.left - padding}px`
    this.highlightEl.style.top = `${rect.top - padding}px`
    this.highlightEl.style.width = `${rect.width + padding * 2}px`
    this.highlightEl.style.height = `${rect.height + padding * 2}px`
    this.highlightEl.style.opacity = '1'
  }

  /**
   * 隐藏元素高亮
   */
  hideHighlight(): void {
    if (!this.highlightEl) return
    this.highlightEl.style.opacity = '0'
  }

  // ============================================================
  // DOM 创建
  // ============================================================

  /** 创建虚拟光标 SVG 元素 */
  private createCursorElement(): HTMLElement {
    const wrapper = document.createElement('div')
    wrapper.id = 'ui-automator-cursor'
    const s = this.options.size

    wrapper.style.cssText = `
      position: fixed;
      left: 0;
      top: 0;
      width: ${s}px;
      height: ${s}px;
      z-index: ${this.options.zIndex};
      pointer-events: none;
      transform: translate3d(${this.posX}px, ${this.posY}px, 0) scale(1);
      transition: none;
      filter: drop-shadow(1px 2px 3px rgba(0,0,0,0.3));
      will-change: transform;
    `

    // 使用 SVG 绘制光标箭头
    wrapper.innerHTML = `
      <svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M5 3L19 12L12 13L9 20L5 3Z" fill="${this.resolvedColor}" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>
      </svg>
    `

    return wrapper
  }

  /** 创建目标高亮元素 */
  private createHighlightElement(): HTMLElement {
    const el = document.createElement('div')
    el.id = 'ui-automator-highlight'
    el.style.cssText = `
      position: fixed;
      left: 0;
      top: 0;
      width: 0;
      height: 0;
      border: 2px solid ${this.resolvedColor};
      border-radius: 4px;
      background: ${this.resolvedColor}15;
      pointer-events: none;
      z-index: ${this.options.zIndex - 1};
      opacity: 0;
      transition: all 0.25s ease, opacity 0.2s ease;
      box-shadow: 0 0 0 1px ${this.resolvedColor}30;
    `
    return el
  }

  // ============================================================
  // 注入全局样式（波纹动画关键帧）
  // ============================================================

  /** 确保波纹动画 CSS 已注入到页面 */
  static injectStyles(): void {
    const styleId = 'ui-automator-visual-styles'
    if (document.getElementById(styleId)) return

    const style = document.createElement('style')
    style.id = styleId
    style.textContent = `
      @keyframes ui-automator-ripple {
        0% {
          transform: scale(0.5);
          opacity: 0.4;
        }
        100% {
          transform: scale(2.5);
          opacity: 0;
        }
      }
      @keyframes ui-automator-pulse {
        0%, 100% { box-shadow: 0 0 0 0 currentColor; }
        50% { box-shadow: 0 0 0 6px transparent; }
      }
    `
    document.head.appendChild(style)
  }
}
