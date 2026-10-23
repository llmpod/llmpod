import {
  defineConfig,
  presetWind3,
  presetAttributify,
  transformerDirectives,
  transformerVariantGroup,
} from 'unocss'
import presetRemToPx from '@unocss/preset-rem-to-px'

export default defineConfig({
  presets: [presetWind3(), presetAttributify(), presetRemToPx({ baseFontSize: 10 })],
  transformers: [transformerDirectives(), transformerVariantGroup()],
  blocklist: [/\$\{[^}]*\}/],
  // 运行时返回的语义色类(useStore.ts 的 progressColor 返回 bg-*,successRateTextClass 返回 text-*-text):
  // UnoCSS 默认只扫 .vue/.tsx 等,普通 .ts 里以字符串形式返回的类名扫不到,需 safelist 显式生成
  safelist: [
    'bg-success',
    'bg-warning',
    'bg-error',
    'bg-info',
    'text-success-text',
    'text-warning-text',
    'text-error-text',
    // 首次部署引导弹窗标题(components/firstGuide.ts 的 useDialog title 渲染函数内构造,同类场景)
    'text-[2.4rem]',
  ],
  theme: {
    // 字号:显式 px 值,规避 presetRemToPx(baseFontSize=10) 把 rem 类缩放到 62.5% 的问题
    // 保证 text-xs/sm/lg/xl/2xl 对应设计稿常规字号 12/14/16/18/20/24px
    fontSize: {
      xs: ['12px', '1.2'],
      sm: ['14px', '1.4'],
      lg: ['16px', '1.6'],
      xl: ['18px', '1.38'],
      '2xl': ['20px', '2'],
    },
    colors: {
      // === 动态主题色:消费完整色值变量 --c-*(由 useTheme 写入,跟随 theme-lab 改色联动)===
      // 不用 rgb(var(--n-*-color)):--n-*-color 是逗号串 RGB,经 rgb() 包裹会被 UnoCSS 拆成
      // rgb(var(--n-*-color) / opacity) 非法语法(逗号串+空格 alpha)而失效。
      primary: 'var(--c-primary)',
      info: 'var(--c-info)',
      success: 'var(--c-success)',
      warning: 'var(--c-warning)',
      error: 'var(--c-error)',
      // === 主色软底 Fill-14:主色 10% 透明,跟随主题色
      'primary-soft': 'rgba(var(--n-primary-color), 0.1)',

      // === 文字安全语义色:原语义色当文字色仅 2.62–3.23:1,不达正文 4.5:1 ===
      // 窄屏卡片的小字(金额方向/状态)用它;--c-success 等原值仍用于底色与描边
      'success-text': 'var(--c-success-text)',
      'error-text': 'var(--c-error-text)',
      'warning-text': 'var(--c-warning-text)',
      'info-text': 'var(--c-info-text)',

      // === 中性文字色 Text ===
      strong: 'var(--c-strong)', // Text-5 标题/强调
      default: 'var(--c-default)', // Text-4 正文(默认)
      secondary: 'var(--c-secondary)', // Text-3 次要
      tertiary: 'var(--c-tertiary)', // Text-2 占位
      disabled: 'var(--c-disabled)', // 禁用态
      inverse: 'var(--c-inverse)', // Text-1 反色
      icon: 'var(--c-icon)', // Fill-11 图标

      // === 线条色 Line ===
      line: 'var(--c-line)', // Line-1 分割线
      'line-strong': 'var(--c-line-strong)', // Line-2 边框
      'line-glass': 'var(--c-line-glass)', // Line-3 玻璃边

      // === 填充/背景 Fill / Background(按灰阶层级编号:1 最浅 → 4 渐深)===
      'fill-1': 'var(--c-fill-1)', // Fill-1 白底容器
      'fill-2': 'var(--c-fill-2)', // Fill-2 表头
      'fill-3': 'var(--c-fill-3)', // Fill-3 侧栏选中
      'fill-4': 'var(--c-fill-4)', // Fill-4 次级底
      page: 'var(--c-page)', // Background 页面背景
      'brand-dark': 'var(--c-brand-dark)', // Brand2 登录页深底
      glass: 'var(--c-glass)', // Fill-15 卡片玻璃底

      // === Tag 底色(固定语义) ===
      'tag-cyan': 'var(--c-tag-cyan)',
      'tag-red': 'var(--c-tag-red)',
      'tag-purple': 'var(--c-tag-purple)',
      'tag-pink': 'var(--c-tag-pink)',
      'tag-blue': 'var(--c-tag-blue)',
      'tag-teal': 'var(--c-tag-teal)',

      // === 装饰/状态固定色 ===
      'accent-blue': 'var(--c-accent-blue)', // Text-9 蓝
      'accent-teal': 'var(--c-accent-teal)', // Text-10 青
      switch: 'var(--c-switch)', // Fill-12 开关
      progress: 'var(--c-progress)', // Fill-13 进度条
    },
  },
  shortcuts: {
    // 字号业务别名:口语化入口,指向标准档位(经 theme.fontSize 已锁定为 14px/18px)
    'text-base': 'text-xs', // 12px
    'text-small': 'text-sm', // 14px
    'text-large': 'text-lg', // 18px
  },
})
