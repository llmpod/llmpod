import { colord } from 'colord'
import { kebabCase } from 'lodash-es'
import { commonDark, commonLight } from 'naive-ui'

import { generatePalette, getTextColor, getThemeColors } from './palette'
import type { ThemeColorKey, ThemeColorType, ThemeConfig } from './types'

type CssObject = Record<string, string>

/** 将 CSS style 文本解析为键值对象。 */
export function parseCssText(cssText: string): CssObject {
  const cssObject: CssObject = {}
  for (const rule of cssText.split(';')) {
    const separator = rule.indexOf(':')
    if (separator < 0) continue
    const key = rule.slice(0, separator).trim()
    const value = rule.slice(separator + 1).trim()
    if (key) cssObject[key] = value
  }
  return cssObject
}

/**
 * 将主题色板以 RGB CSS 变量写入文档根节点。
 * 这是 theme 模块唯一的 DOM 写入口,由 useTheme 在主题依赖变化时显式调用。
 */
export function addCssVarsToHtml(config: ThemeConfig, darkMode: boolean): void {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  const cssObject = parseCssText(root.style.cssText)
  const configCssObject: CssObject = {}

  // 文档根背景与文字色,供非 Naive 区域(body、原生控件)跟随主题
  const bodyColor = darkMode ? commonDark.bodyColor : commonLight.bodyColor
  const textColor = getTextColor(darkMode)
  const bodyRgb = colord(bodyColor).toRgb()
  const textRgb = colord(textColor).toRgb()
  configCssObject['--n-body-color'] = `${bodyRgb.r},${bodyRgb.g},${bodyRgb.b}`
  configCssObject['--n-text-color'] = `${textRgb.r},${textRgb.g},${textRgb.b}`
  // 全局默认边框色(完整色值),供 reset 的全元素 border 默认值与业务样式 var(--n-border-color) 消费;
  // 取 Naive UI common 的 borderColor,随明暗主题切换
  configCssObject['--n-border-color'] = darkMode ? commonDark.borderColor : commonLight.borderColor

  const themeColors = getThemeColors(config, darkMode)
  for (const [key, value] of Object.entries(themeColors) as [ThemeColorKey, string][]) {
    const { r, g, b } = colord(value).toRgb()
    configCssObject[`--n-${kebabCase(key)}`] = `${r},${g},${b}`
  }
  for (const [key, value] of Object.entries(config) as [ThemeColorType, string][]) {
    // 完整色值变量:供 UnoCSS 的 bg-primary/text-primary 等工具类直接消费。
    // --n-*-color 是逗号串 RGB,被 rgb() 包裹后 UnoCSS 会拆 opacity 生成非法语法,
    // 故另存一份完整 hex 到 --c-*,与 tokens.scss 的 --c-* (完整色值) 命名约定一致。
    configCssObject[`--c-${kebabCase(key)}`] = value
    generatePalette(value, darkMode).forEach((color, index) => {
      const { r, g, b } = colord(color).toRgb()
      configCssObject[`--n-${key}-color-${index + 1}`] = `${r},${g},${b}`
    })
  }

  root.style.cssText = Object.entries({ ...cssObject, ...configCssObject })
    .map(([key, value]) => `${key}: ${value};`)
    .join(' ')
}
