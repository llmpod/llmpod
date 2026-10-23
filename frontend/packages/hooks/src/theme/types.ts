/** 支持配置的主题色类型。 */
export type ThemeColorType = 'primary' | 'info' | 'success' | 'warning' | 'error'

/** 主题色类型的首字母大写形式。 */
export type ThemeColorTypeCase = 'Primary' | 'Info' | 'Success' | 'Warning' | 'Error'

/** Naive UI 通用主题色状态。 */
export type ThemeColorSceneCase = '' | 'Suppl' | 'Hover' | 'Pressed'

/** Naive UI 按钮主题色状态。 */
export type ButtonColorSceneCase = '' | 'Hover' | 'Pressed' | 'Focus' | 'Disabled'

/** 应用主题颜色配置。 */
export type ThemeConfig = Record<ThemeColorType, string>

/** 应用支持的主题模式。 */
export type ThemeMode = 'light' | 'dark' | 'auto'

/** Naive UI 通用主题色键。 */
export type ThemeColorKey = `${ThemeColorType}Color${ThemeColorSceneCase}`

/** 主题色计算结果。 */
export type ThemeColors = Partial<Record<ThemeColorKey, string>>

/** 各主题色对应的十阶色板。 */
export type ThemePalettes = Record<ThemeColorType, string[]>

/**
 * 后端下发的主题配置(独立接口,登录页也需要,故不依赖登录态)。
 * 所有字段可选:后端只管自己要管的,未下发的字段继续走本地配置。
 * 明暗模式不消费:纯前端本地偏好(app_theme),由用户经 LmThemeModeCard/setThemeMode 自主切换。
 */
export interface RemoteThemeConfig {
  /** 品牌主色(hex),优先于本地 themeConfig.primary */
  primary?: string
  /** 登录页背景图地址,空字符串表示恢复默认背景 */
  loginBackground?: string
  /** 登录页主色(hex),空/未下发时回落全站 primary */
  loginPrimary?: string
}
