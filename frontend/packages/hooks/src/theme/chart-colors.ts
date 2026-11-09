import { colord } from 'colord'

import { DEFAULT_THEME_CONFIG } from './constants'

/**
 * 图表取色工具。
 *
 * echarts 的 option 只吃完整色值,吃不到 CSS 变量,所以渲染前必须把样式表里的值读出来。
 * 这一层被运营端三屏(总览 / 引擎 / 统计)与租户端工作台共同消费,重复实现会让
 * 「变量缺失时的兜底色」各自漂移,故收敛到这里。
 */

/**
 * tokens.scss 中 token 的亮色默认值。
 *
 * 仅在样式未加载、变量缺失时兜底。必须与 tokens.scss 同名 token 取值一致 ——
 * 兜底色与 token 不同,缺失场景就会画出另一套配色,比不兜底更难查。
 */
export const CHART_FALLBACK = {
  /** --c-secondary 次要信息 */
  secondary: '#a8a8a8',
  /** --c-line 分割线 */
  line: '#f2f2f2',
} as const

/** 默认主色的 rgb() 形式:由 DEFAULT_THEME_CONFIG.primary 推导,避免再抄一份色值 */
const DEFAULT_PRIMARY_RGB = (() => {
  const { r, g, b } = colord(DEFAULT_THEME_CONFIG.primary).toRgb()
  return `rgb(${r}, ${g}, ${b})`
})()

/** 读取 CSS 变量;变量为「r, g, b 逗号串」(如 --n-*-color)时拼成 rgb() 供 echarts 使用 */
export function cssRgb(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value ? `rgb(${value})` : fallback
}

/** 读取 CSS 变量;变量为完整色值(如 --c-*)时原样返回 */
export function cssVar(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

/** 运行时主题主色(--n-primary-color 由 addCssVarsToHtml 注入) */
export function primaryColor(): string {
  return cssRgb('--n-primary-color', DEFAULT_PRIMARY_RGB)
}

/** 轴线/网格通用配色(跟随明暗主题) */
export function chartAxisColors() {
  return {
    axisColor: cssVar('--c-secondary', CHART_FALLBACK.secondary),
    splitColor: cssVar('--c-line', CHART_FALLBACK.line),
  }
}
