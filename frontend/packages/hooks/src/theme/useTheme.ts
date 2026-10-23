import { computed, watch, type Ref } from 'vue'
import {
  createSharedComposable,
  useColorMode,
  useCycleList,
  useStorage,
  type BasicColorSchema,
} from '@vueuse/core'
import { darkTheme } from 'naive-ui'

import {
  DEFAULT_THEME_CONFIG,
  HEX_COLOR_RE,
  REMOTE_THEME_STORAGE_KEY,
  THEME_CONFIG_STORAGE_KEY,
  THEME_MODE_LIST,
  THEME_STORAGE_KEY,
} from './constants'
import { addCssVarsToHtml } from './css-vars'
import { generatePalette } from './palette'
import { getThemeOverrides } from './overrides'
import type { RemoteThemeConfig, ThemeColorType, ThemeConfig, ThemeMode, ThemePalettes } from './types'

/** 兜底明暗模式(存储值非法时回落)。 */
const DEFAULT_THEME_MODE: ThemeMode = 'auto'

/**
 * 校验并净化后端主题配置:非法 primary/loginPrimary 直接丢弃,合法字段原样保留。
 * mode 不在消费范围(明暗为纯前端本地偏好,由 LmThemeModeCard / setThemeMode 持久化)。
 * 由 initRemoteTheme 与已有缓存合并后写入存储。
 */
function sanitizeRemoteTheme(config: RemoteThemeConfig): Partial<RemoteThemeConfig> {
  const next: Partial<RemoteThemeConfig> = {}
  if (config.primary && HEX_COLOR_RE.test(config.primary)) next.primary = config.primary
  if (typeof config.loginBackground === 'string') next.loginBackground = config.loginBackground
  // loginPrimary:空串=清空(回落 primary),非空必须合法 hex
  if (typeof config.loginPrimary === 'string') {
    if (!config.loginPrimary || HEX_COLOR_RE.test(config.loginPrimary)) {
      next.loginPrimary = config.loginPrimary
    }
  }
  return next
}

/** 全局共享的主题状态和主题配置 Hook。 */
export const useTheme = createSharedComposable(() => {
  const colorMode = useColorMode({
    initialValue: DEFAULT_THEME_MODE,
    emitAuto: true,
    storageKey: THEME_STORAGE_KEY,
  })
  const { state: mode, next } = useCycleList(THEME_MODE_LIST, {
    initialValue: colorMode.store as Ref<ThemeMode>,
  })

  watch(
    mode,
    (value) => {
      const resolvedMode = THEME_MODE_LIST.includes(value) ? value : DEFAULT_THEME_MODE
      if (resolvedMode !== value) mode.value = resolvedMode
      colorMode.value = resolvedMode as BasicColorSchema
    },
    { immediate: true },
  )

  // 反向同步:跨标签页 storage 事件只更新 colorMode.store(useStorage 内部 watch 仅驱动 html class),
  // mode 不跟随会导致 darkMode/theme/themeOverrides 停在旧值,出现暗色 class 叠亮色组件的混合态。
  // 同值赋值不触发 watch,双向链路各自一步收敛,无死循环。
  watch(colorMode.store, (value) => {
    mode.value = THEME_MODE_LIST.includes(value) ? value : DEFAULT_THEME_MODE
  })

  const darkMode = computed(() =>
    mode.value === 'auto' ? colorMode.system.value === 'dark' : mode.value === 'dark',
  )

  // 同步原生配色方案,使滚动条与原生表单控件跟随明暗主题
  watch(
    darkMode,
    (isDark) => {
      if (typeof document === 'undefined') return
      document.documentElement.style.colorScheme = isDark ? 'dark' : 'light'
    },
    { immediate: true },
  )

  // 主题色配置持久化到 localStorage,使刷新或跨页面导航后仍保留用户自定义色
  const themeConfig = useStorage<ThemeConfig>(
    THEME_CONFIG_STORAGE_KEY,
    { ...DEFAULT_THEME_CONFIG },
    undefined,
    { mergeDefaults: true },
  )
  // 后端主题配置的缓存副本:首屏启动脚本(theme.boot.js)与下次冷启动都以此为准,
  // 后端接口失败时也能维持上一次下发的主题
  const remoteTheme = useStorage<RemoteThemeConfig>(REMOTE_THEME_STORAGE_KEY, {})
  // 生效配置 = 本地配置 + 后端覆盖(后端只管 primary,明暗与其余四色走本地/默认)
  const effectiveThemeConfig = computed<ThemeConfig>(() =>
    remoteTheme.value.primary
      ? { ...themeConfig.value, primary: remoteTheme.value.primary }
      : themeConfig.value,
  )
  const loginBackground = computed(() => remoteTheme.value.loginBackground ?? '')
  // 登录页主色回落链:loginPrimary → 全站 primary(后端/本地/默认)
  const loginPrimary = computed(
    () => remoteTheme.value.loginPrimary || effectiveThemeConfig.value.primary,
  )
  const theme = computed(() => (darkMode.value ? darkTheme : null))
  const themeOverrides = computed(() => getThemeOverrides(effectiveThemeConfig.value, darkMode.value))
  const themeColors = computed<ThemePalettes>(() => {
    const colors = {} as ThemePalettes
    for (const [key, value] of Object.entries(effectiveThemeConfig.value) as [
      ThemeColorType,
      string,
    ][]) {
      colors[key] = generatePalette(value, darkMode.value)
    }
    return colors
  })

  // CSS 变量注入独立成显式 watch:主题依赖变化即同步到文档根节点,
  // 不再依赖 themeOverrides computed 被读取才触发副作用
  watch(
    [effectiveThemeConfig, darkMode],
    ([config, isDark]) => {
      addCssVarsToHtml(config, isDark)
    },
    { immediate: true },
  )

  /** 循环切换 dark、light 和 auto 模式。 */
  function toggleDarkMode(): void {
    next()
  }

  /** 显式设置主题模式。 */
  function setThemeMode(value: ThemeMode): void {
    mode.value = value
  }

  /** 合并更新应用主题颜色。 */
  function setThemeConfig(config: Partial<ThemeConfig>): void {
    themeConfig.value = { ...themeConfig.value, ...config }
  }

  /**
   * 接入后端主题配置(登录页之前的独立接口返回后调用)。
   * 逐字段校验后写入缓存:非法 primary/loginPrimary 直接丢弃,后端返回的部分字段与缓存合并;
   * 明暗模式不消费,始终保留本地用户偏好。接口失败时不要调用,缓存继续生效。
   */
  function initRemoteTheme(config: RemoteThemeConfig | null | undefined): void {
    if (!config) return
    remoteTheme.value = { ...remoteTheme.value, ...sanitizeRemoteTheme(config) }
  }

  return {
    darkMode,
    loginBackground,
    loginPrimary,
    mode,
    remoteTheme,
    theme,
    themeConfig,
    themeColors,
    themeOverrides,
    initRemoteTheme,
    setThemeConfig,
    setThemeMode,
    toggleDarkMode,
  }
})
