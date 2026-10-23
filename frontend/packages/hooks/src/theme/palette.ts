import { generate } from '@ant-design/colors'
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
