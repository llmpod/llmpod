import { h } from 'vue'
import {
  BusinessOutline,
  CalculatorOutline,
  CubeOutline,
  DocumentTextOutline,
  GitNetworkOutline,
  HardwareChipOutline,
  InformationCircleOutline,
  KeyOutline,
  ListOutline,
  PersonOutline,
  PricetagsOutline,
  ReceiptOutline,
  RocketOutline,
  ServerOutline,
  SettingsOutline,
  ShieldCheckmarkOutline,
  ShieldOutline,
  SpeedometerOutline,
  StatsChartOutline,
  StorefrontOutline,
  TerminalOutline,
  WarningOutline,
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
  // {
  //   label: '总览大屏',
  //   name: 'dashboard',
  //   icon: groupIcon('admin-1'),
  // },
  // {
  //   label: '调用统计',
  //   name: 'statistics',
  //   icon: groupIcon('admin-1'),
  // },
  { label: '总览大屏', name: 'dashboard', icon: SpeedometerOutline },
  {
    label: '仪表盘',
    key: 'monitor',
    icon: groupIcon('admin-1'),
    children: [
      { label: '调用统计', name: 'statistics', icon: StatsChartOutline },
      { label: '推理引擎', name: 'engine-dashboard', icon: HardwareChipOutline },
    ],
  },
  {
    label: '基础设施',
    key: 'infrastructure',
    icon: groupIcon('admin-2'),
    children: [
      { label: '集群管理', name: 'node-pool', icon: ServerOutline },
      { label: '节点列表', name: 'node-list', icon: ListOutline },
      { label: '模型管理', name: 'deploy', icon: CubeOutline },
      { label: '发布中心', name: 'model-release', icon: RocketOutline },
    ],
  },
  {
    label: '租户',
    key: 'tenant',
    icon: groupIcon('admin-4'),
    defaultExpanded: false,
    // 租户概览已并入本页(顶部统计卡 + 列表),单入口「租户管理」
    children: [{ label: '租户管理', name: 'tenant-management', icon: BusinessOutline }],
  },
  {
    label: '用户与权限',
    key: 'user-permission',
    icon: groupIcon('admin-5'),
    defaultExpanded: false,
    children: [
      { label: '用户管理', name: 'user', icon: PersonOutline },
      { label: '角色管理', name: 'role', icon: ShieldCheckmarkOutline },
    ],
  },
  {
    label: '开发者工具',
    key: 'dev-tools',
    icon: groupIcon('admin-6'),
    defaultExpanded: false,
    children: [
      { label: '在线调试', name: 'playground', icon: TerminalOutline },
      { label: '模型市场', name: 'model-mark', icon: StorefrontOutline },
      { label: 'API Key', name: 'api-key', icon: KeyOutline },
    ],
  },
  {
    label: '审计和计费',
    key: 'audit-billing',
    icon: groupIcon('admin-7'),
    defaultExpanded: false,
    children: [
      { label: '审计日志', name: 'logs', icon: DocumentTextOutline },
      { label: '套餐管理', name: 'package', icon: PricetagsOutline },
      { label: '订单管理', name: 'orders', icon: ReceiptOutline },
      { label: '计量计费', name: 'bills', icon: CalculatorOutline },
      { label: '费用告警', name: 'alarm', icon: WarningOutline },
    ],
  },
  {
    label: '系统',
    key: 'system',
    icon: groupIcon('admin-9'),
    defaultExpanded: false,
    children: [
      // 通用设置 + 品牌配置已合并为本页两个子 tab(?tab=general/brand)
      { label: '设置', name: 'config', icon: SettingsOutline },
      { label: 'SSO配置', name: 'sso', icon: ShieldOutline },
      { label: 'Webhook配置', name: 'webhook', icon: GitNetworkOutline },
      { label: '关于', name: 'about', icon: InformationCircleOutline },
    ],
  },
]
