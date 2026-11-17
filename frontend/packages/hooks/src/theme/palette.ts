import { generate } from '@ant-design/colors'
import { colord } from 'colord'
import { commonDark, commonLight } from 'naive-ui'

import type { ThemeColorType, ThemeColors, ThemeConfig } from './types'

/** 色板缓存:key 为 `light:颜色` / `dark:颜色`,避免同一颜色在单次主题切换中被重复生成。 */
const paletteCache = new Map<string, string[]>()

/** 根据基础颜色生成明色或暗色十阶色板。 */
export function generatePalette(color: string, darkMode: boolean): string[] {
  const cacheKey = `${darkMode ? 'dark' : 'light'}:${color}`
  let palette = paletteCache.get(cacheKey)
  if (!palette) {
    palette = darkMode
      ? generate(color, { theme: 'dark', backgroundColor: commonDark.bodyColor })
      : generate(color)
    paletteCache.set(cacheKey, palette)
  }
  return palette
}

/** Naive UI 明暗主题的基础文本色(表单类部件、开关按钮等复用)。 */
export function getTextColor(darkMode: boolean): string {
  return darkMode ? commonDark.textColor2 : commonLight.baseColor
}

interface ColorScene {
  scene: '' | 'Suppl' | 'Hover' | 'Pressed'
  /** 十阶色板中的取色下标。 */
  index: number
}

/** 将主题配置转换为 Naive UI 通用颜色状态。 */
export function getThemeColors(config: ThemeConfig, darkMode: boolean): ThemeColors {
  const themeColors: ThemeColors = {}
  const colorScenes: ColorScene[] = [
    { scene: '', index: 5 },
    { scene: 'Hover', index: 4 },
    { scene: 'Suppl', index: 4 },
    { scene: 'Pressed', index: 6 },
  ]
  for (const key of Object.keys(config) as ThemeColorType[]) {
    const palette = generatePalette(config[key], darkMode)
    for (const { scene, index } of colorScenes) {
      themeColors[`${key}Color${scene}`] = palette[index]!
    }
  }
  return themeColors
}

// ---------------------------------------------------------------------------
// 正文级主色文字(--c-primary-text)
// ---------------------------------------------------------------------------

/**
 * 容器底色(与 tokens.scss 的 --c-fill-1 同值)。
 * CSS 变量无法参与 JS 对比度计算,故此处必须落到具体色值 —— tokens.scss 改 --c-fill-1 时同步这里。
 */
const SURFACE_LIGHT = '#ffffff'
const SURFACE_DARK = '#18181c'

/** WCAG AA 正文对比度门槛。 */
const TEXT_CONTRAST_TARGET = 4.5

/** WCAG 相对亮度:0(黑)–1(白),按 sRGB 线性化后加权。 */
function relativeLuminance(color: string): number {
  const { r, g, b } = colord(color).toRgb()
  const linear = (channel: number): number => {
    const srgb = channel / 255
    return srgb <= 0.03928 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
}

/** 两色对比度(WCAG 2.x):1–21,与前景/背景顺序无关。 */
function contrastRatio(a: string, b: string): number {
  const light = Math.max(relativeLuminance(a), relativeLuminance(b))
  const dark = Math.min(relativeLuminance(a), relativeLuminance(b))
  return (light + 0.05) / (dark + 0.05)
}

/**
 * 求解主题色的「正文级」变体:保持色相与彩度不变,只沿 HSL 的 L 轴朝远离容器底的方向
 * 逐步移动,直到达到正文 4.5:1。主题色本身继续用于底色/描边/图标,两者职责分离。
 *
 * 消费方是 `--c-primary-text`(菜单选中项文字、卡片内主色按钮等作正文色的位置)。
 * 该值必须由运行时注入而非 CSS 静态声明:静态值无法随主题色联动,
 * 暗色下曾因硬编码固定蓝导致换主题色后菜单文字仍为蓝色。
 */
export function getPrimaryTextColor(color: string, darkMode: boolean): string {
  const surface = darkMode ? SURFACE_DARK : SURFACE_LIGHT
  if (contrastRatio(color, surface) >= TEXT_CONTRAST_TARGET) return colord(color).toHex()
  // 暗底提亮、亮底压深;步长 0.02 保证临界值附近不会跳过首个达标档位
  const step = darkMode ? 0.02 : -0.02
  let current = color
  // 50 步(±100% L)足以把任意合法色推到纯黑/纯白,兼作非法色值的终止兜底
  for (let index = 0; index < 50; index += 1) {
    current = step > 0 ? colord(current).lighten(step).toHex() : colord(current).darken(-step).toHex()
    if (contrastRatio(current, surface) >= TEXT_CONTRAST_TARGET) break
  }
  return current
}
