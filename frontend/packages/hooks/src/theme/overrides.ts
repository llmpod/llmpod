import { kebabCase, upperFirst } from 'lodash-es'
import { commonDark, commonLight, type GlobalThemeOverrides } from 'naive-ui'

import { THEME_COLOR_TYPES } from './constants'
import { getTextColor, getThemeColors } from './palette'
import type { ButtonColorSceneCase, ThemeColorTypeCase, ThemeConfig } from './types'

type ButtonColorKey = `textColor${ButtonColorSceneCase}${ThemeColorTypeCase}`

// ---------------------------------------------------------------------------
// 静态层:与明暗/主题色无关的设计 token(尺寸/字号/边框),模块级常量避免每次切换重建
// ---------------------------------------------------------------------------

const STATIC_COMMON_OVERRIDES = {
  // 表单类组件(Input/Select/Cascader/TreeSelect/InputNumber/DatePicker 等)的
  // medium 尺寸高度均引用 common.heightMedium,默认 34px,此处收敛为 32px。
  heightMedium: '32px',
  borderColor: 'var(--c-line)',
  textColor1: 'var(--c-strong)',
  textColor2: 'var(--c-default)',
  textColor3: 'var(--c-tertiary)',
  textColorDisabled: 'var(--c-disabled)',
}

const STATIC_TABS_OVERRIDES = {
  tabPaddingLargeLine: '1.6rem 0',
  tabFontSizeLarge: '1.4rem',
  tabBorderColor: 'transparent',
}

const STATIC_DESCRIPTIONS_OVERRIDES = {
  fontSizeSmall: '1.2rem',
}

const STATIC_DROPDOWN_OVERRIDES = {
  fontSizeSmall: '1.2rem',
}

const STATIC_BUTTON_OVERRIDES = {
  paddingMedium: '0 1.2rem',
  iconSizeMedium: '1.6rem',
  fontSizeMedium: '1.2rem',
  fontSizeSmall: '1.2rem',
}

const STATIC_INPUT_OVERRIDES = {
  boxShadowFocus: 'none',
  fontSizeMedium: '1.2rem',
  textColor: 'var(--c-default)',
  placeholderTextColor: 'var(--c-tertiary)',
  border: '1px solid var(--c-line)',
}

const STATIC_CARD_OVERRIDES = {
  titleTextColor: 'var(--c-strong)',
  paddingMedium: '14px 20px',
  titleFontSizeMedium: '1.4rem',
}

const STATIC_DATATABLE_OVERRIDES = {
  thTextColor: 'var(--c-default)',
  tdTextColor: 'var(--c-default)',
  borderColor: 'var(--c-line)',
  thFontWeight: '700',
  fontSizeMedium: '1.2rem',
}

const STATIC_FORM_OVERRIDES = {
  feedbackFontSizeMedium: '1.2rem',
  labelFontSizeTopMedium: '1.2rem',
  labelFontSizeLeftMedium: '1.2rem',
  feedbackTextColor: 'var(--c-secondary)',
  labelTextColor: 'var(--c-default)',
}

// ---------------------------------------------------------------------------
// 动态层:依赖明暗模式(或后端主题色)的覆盖,按组件收拢为小函数
// ---------------------------------------------------------------------------

/**
 * 组装与主题色值无关、仅随明暗切换的组件级颜色修正:
 * Button 文字色矩阵、日历/日期选择/开关的部件基准色。
 */
function getOtherColor(darkMode: boolean) {
  const textColor = getTextColor(darkMode)
  const buttonOverrides = {} as Record<ButtonColorKey, string>
  const scenes: ButtonColorSceneCase[] = ['', 'Hover', 'Pressed', 'Focus', 'Disabled']
  for (const key of THEME_COLOR_TYPES) {
    for (const scene of scenes) {
      const colorKey = `textColor${scene}${upperFirst(key)}` as ButtonColorKey
      // 暗色模式下 primary 实色按钮文字统一使用纯白,确保主题色背景上文字的对比度;
      // disabled 态保持 textColor 的弱化色,维持禁用语义
      if (darkMode && key === 'primary' && scene !== 'Disabled') {
        buttonOverrides[colorKey] = '#fff'
        continue
      }
      buttonOverrides[colorKey] = textColor
    }
  }
  return {
    Button: buttonOverrides,
    DatePicker: { itemTextColorActive: textColor },
    Calendar: { dateTextColorCurrent: textColor },
    Switch: { buttonColor: textColor },
  }
}

/** 为 n-alert 按主题色覆盖五种类型:Naive UI 默认 self theme 使用内置色板,
 * 且不包含 *Primary 变量(`<LmAlert type="primary">` 会全部丢色)。
 * 此处统一替换:背景/边框/图标/标题/正文复用运行时 `--n-{type}-color`(addCssVarsToHtml 注入的
 * RGB 串,半透明叠加可自动适配明暗),使五种类型的文字与图标均为对应主题色,而非内置色板;
 * primary 另需补齐关闭按钮(Naive self 无对应默认键,取 common 明暗值)。
 * 注:GlobalThemeOverrides.Alert 类型不含 *Primary 键,故经 Record 断言后注入,运行时按 key 命中。 */
function getAlertOverrides(darkMode: boolean): GlobalThemeOverrides['Alert'] {
  const common = darkMode ? commonDark : commonLight
  const alertOverrides: Record<string, string> = {}
  const types: ThemeColorTypeCase[] = ['Primary', 'Info', 'Success', 'Warning', 'Error']
  for (const type of types) {
    const rgb = `var(--n-${kebabCase(type)}-color)`
    alertOverrides[`color${type}`] = `rgba(${rgb}, 0.08)`
    alertOverrides[`border${type}`] = `1px solid rgba(${rgb}, 0.25)`
    alertOverrides[`iconColor${type}`] = `rgb(${rgb})`
    alertOverrides[`titleTextColor${type}`] = `rgb(${rgb})`
    alertOverrides[`contentTextColor${type}`] = `rgb(${rgb})`
  }
  if (darkMode) {
    // 暗色下 primary 半透明主题色背景对比度不足,改用次级容器底 --c-fill-4
    alertOverrides.colorPrimary = 'var(--c-fill-4)'
    alertOverrides.contentTextColorPrimary = 'var(--c-default)'
  }
  Object.assign(alertOverrides, {
    closeColorHoverPrimary: common.closeColorHover,
    closeColorPressedPrimary: common.closeColorPressed,
    closeIconColorPrimary: common.closeIconColor,
    closeIconColorHoverPrimary: common.closeIconColorHover,
    closeIconColorPressedPrimary: common.closeIconColorPressed,
    fontSize: '1.2rem',
  })
  return alertOverrides as GlobalThemeOverrides['Alert']
}

function getMenuOverrides(darkMode: boolean): GlobalThemeOverrides['Menu'] {
  return {
    // 侧栏菜单项高:取自导航设计稿(36px),与 1px 项间距共同构成 37px 的行节奏;
    // 该值是侧栏菜单的尺寸基准,组件层不再用 !important 覆盖,改这里即全局生效
    itemHeight: '3.6rem',
    // 暗色下默认项降一档(--c-secondary)弱化常驻文字,激活项升回正文色(--c-default)以拉开层级;
    // 亮色沿用默认/正文对比即可。
    itemTextColor: darkMode ? 'var(--c-secondary)' : 'var(--c-default)',
    itemTextColorHover: 'var(--c-primary)',
    ...(darkMode
      ? {
          itemIconColor: 'var(--c-secondary)',
          itemIconColorChildActive: 'var(--c-default)',
          itemTextColorChildActive: 'var(--c-default)',
          itemTextColorActive: 'var(--c-default)',
          arrowColorActive: 'var(--c-default)',
          arrowColorChildActive: 'var(--c-default)',
          itemColorActive: 'var(--c-fill-4)',
          itemTextColorActiveHover: 'var(--c-default)',
          itemTextColorChildActiveHover: 'var(--c-default)',
          itemIconColorChildActiveHover: 'var(--c-default)',
          arrowColorChildActiveHover: 'var(--c-default)',
          itemTextColorHover: 'var(--c-default)',
        }
      : {}),
  }
}

function getInputOverrides(darkMode: boolean): GlobalThemeOverrides['Input'] {
  return {
    ...STATIC_INPUT_OVERRIDES,
    ...(darkMode
      ? {
          color: 'transparent',
          colorFocus: 'transparent',
          colorFocusError: 'transparent',
          colorFocusWarning: 'transparent',
        }
      : {}),
  }
}

function getInputNumberOverrides(darkMode: boolean): GlobalThemeOverrides['InputNumber'] {
  return {
    // iconColorDisabled 默认引用 common.textColorDisabled(上方覆盖为 var(--c-disabled)),
    // InputNumber 内部会用 seemly rgba() 解析该值计算按钮禁用态,var() 无法解析直接抛错,
    // 此处改传实色值(与 tokens.scss 的 --c-disabled 明暗取值保持一致)。
    iconColorDisabled: darkMode ? 'rgba(255, 255, 255, 0.35)' : '#c7c7c7',
  }
}

function getCheckboxOverrides(darkMode: boolean): GlobalThemeOverrides['Checkbox'] {
  return {
    fontSizeMedium: '1.2rem',
    ...(darkMode
      ? {
          checkMarkColor: '#fff',
        }
      : {}),
  }
}

function getDataTableOverrides(darkMode: boolean): GlobalThemeOverrides['DataTable'] {
  return {
    ...STATIC_DATATABLE_OVERRIDES,
    thColor: darkMode ? '#1a1a1a' : 'var(--c-fill-2)',
  }
}

function getRadioOverrides(darkMode: boolean): GlobalThemeOverrides['Radio'] {
  return darkMode ? { buttonTextColorActive: '#fff' } : {}
}

function getStepsOverrides(darkMode: boolean): GlobalThemeOverrides['Steps'] {
  return darkMode ? { indicatorTextColorProcess: 'var(--c-strong)' } : {}
}

function getDrawerOverrides(darkMode: boolean): GlobalThemeOverrides['Drawer'] {
  // 暗色下抽屉面板底色加深,避免与页面背景同色导致层级不清
  return darkMode ? { color: '#141414' } : {}
}

// ---------------------------------------------------------------------------
// 组装:纯计算,不产生任何 DOM 副作用(CSS 变量注入见 css-vars.ts 的 addCssVarsToHtml)
// ---------------------------------------------------------------------------

/** 创建 Naive UI 全局主题覆盖。 */
export function getThemeOverrides(config: ThemeConfig, darkMode: boolean): GlobalThemeOverrides {
  const otherColor = getOtherColor(darkMode)
  return {
    common: {
      ...getThemeColors(config, darkMode),
      ...STATIC_COMMON_OVERRIDES,
    },
    ...otherColor,
    Alert: getAlertOverrides(darkMode),
    Tabs: STATIC_TABS_OVERRIDES,
    Menu: getMenuOverrides(darkMode),
    Descriptions: STATIC_DESCRIPTIONS_OVERRIDES,
    Dropdown: STATIC_DROPDOWN_OVERRIDES,
    Radio: getRadioOverrides(darkMode),
    Checkbox: getCheckboxOverrides(darkMode),
    Button: {
      ...otherColor.Button,
      ...STATIC_BUTTON_OVERRIDES,
    },
    Input: getInputOverrides(darkMode),
    InputNumber: getInputNumberOverrides(darkMode),
    Card: STATIC_CARD_OVERRIDES,
    Steps: getStepsOverrides(darkMode),
    Drawer: getDrawerOverrides(darkMode),
    DataTable: getDataTableOverrides(darkMode),
    Form: STATIC_FORM_OVERRIDES,
  }
}
