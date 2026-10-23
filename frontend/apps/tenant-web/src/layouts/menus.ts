import { h } from 'vue'
import {
  DocumentTextOutline,
  InformationCircleOutline,
  KeyOutline,
  PeopleOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
  SpeedometerOutline,
  StatsChartOutline,
  StorefrontOutline,
  TerminalOutline,
  WalletOutline,
} from '@vicons/ionicons5'
import type { LayoutMenuGroup } from '@repo/naive-ui'

/** 菜单分组图标:public/icons/menus 下的 svg,用 mask 渲染为可着色图标,跟随主题色 */
function groupIcon(name: string) {
  const src = `${import.meta.env.BASE_URL}icons/menus/${name}.svg`
  return () =>
    h('div', {
      // 尺寸并入内联 style:h() 渲染函数里的 class 依赖 UnoCSS 静态扫描,管线未覆盖到时规则不生成;
      // 1.6rem=16px 与 LmLayout 侧栏菜单的图标方框等值,保证两端图标尺寸一致
      style: {
        width: '1.6rem',
        height: '1.6rem',
        backgroundColor: 'currentColor',
        mask: `url(${src}) center/contain no-repeat`,
        WebkitMask: `url(${src}) center/contain no-repeat`,
      },
    })
}

/**
 * 侧栏菜单声明:分组与子项的排序/文案/图标的唯一事实源。
 * BasicLayout 渲染侧栏;设置页「模块管理」tab 用它对齐 /auth/menus 字典 key,
 * 保证自助开关列表与侧栏入口一一对应(文案不与后端重复维护)。
 * 子项图标统一用 @vicons/ionicons5 的线性图标,与分组图标同为 16px 方框内尺寸。
 */
export const allMenus: LayoutMenuGroup[] = [
  {
    label: '核心功能',
    key: 'core',
    icon: groupIcon('tenant-1'),
    children: [
      { label: '控制台', name: 'console', icon: SpeedometerOutline },
      { label: '模型市场', name: 'model-mark', icon: StorefrontOutline },
      { label: '在线调试', name: 'playground', icon: TerminalOutline },
      { label: 'API Key', name: 'api-key', icon: KeyOutline },
    ],
  },
  {
    label: '数据与费用',
    key: 'data-cost',
    icon: groupIcon('tenant-2'),
    children: [
      { label: '调用统计', name: 'statistics', icon: StatsChartOutline },
      { label: '费用中心', name: 'cost', icon: WalletOutline },
    ],
  },
  {
    label: '管理',
    key: 'management',
    icon: groupIcon('tenant-3'),
    children: [
      { label: '团队管理', name: 'team', icon: PeopleOutline },
      { label: '审计日志', name: 'logs', icon: DocumentTextOutline },
      { label: '角色权限', name: 'auth', icon: ShieldCheckmarkOutline },
    ],
  },
  {
    label: '其他',
    key: 'other',
    icon: groupIcon('tenant-4'),
    children: [
      // 个人设置页(?tab=profile);关于已独立为本分组下的菜单页
      { label: '设置', name: 'config', icon: SettingsOutline },
      { label: '关于', name: 'about', icon: InformationCircleOutline },
    ],
  },
]
