import type { ThemeConfig, ThemeMode, ThemeColorType } from './types'

/** 默认应用主题颜色。 */
export const DEFAULT_THEME_CONFIG: ThemeConfig = {
  primary: '#1261a8',
  info: '#447FFE',
  success: '#20A53A',
  warning: '#E88915',
  error: '#FF5555',
}

/** 主题色类型全集(与 ThemeColorType 保持一致)。 */
export const THEME_COLOR_TYPES: readonly ThemeColorType[] = [
  'primary',
  'info',
  'success',
  'warning',
  'error',
]

/** 明暗模式循环切换顺序(useCycleList 需要可变数组类型,运行时不应被修改)。 */
export const THEME_MODE_LIST: ThemeMode[] = ['dark', 'light', 'auto']

/**
 * 明暗模式偏好的存储键。
 * 与根目录 theme.boot.js 保持一致,改动需两边同步。
 */
export const THEME_STORAGE_KEY = 'app_theme'

/**
 * 主题色配置的存储键。
 * 与根目录 theme.boot.js 保持一致,改动需两边同步。
 */
export const THEME_CONFIG_STORAGE_KEY = 'app_theme_config'

/**
 * 后端主题配置的本地缓存键:index.html 启动脚本(theme.boot.js)同步读取它做首屏防闪。
 * 与根目录 theme.boot.js 保持一致,改动需两边同步。
 */
export const REMOTE_THEME_STORAGE_KEY = 'app_remote_theme'

/** 校验 3/6 位 hex 颜色(@ant-design/colors 不支持 8 位带 alpha 的 hex)。 */
export const HEX_COLOR_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/
