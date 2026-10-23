/**
 * 前端隐藏路由名单:临时下线页面的统一开关,按路由 name 匹配。
 *
 * 名单内路由在三处统一不可见(均收口到 auth.hasMenu):
 * - 左侧菜单:BasicLayout 菜单过滤按 hasMenu 拦下
 * - 路由访问:router guard 按 hasMenu 拦截,直接访问 URL 提示无权限并落到首个可见菜单
 * - 全局搜索:页面项(派生自菜单)/操作项(search.ts)/子 tab(tabs.ts)均按 hasMenu 过滤,
 *   「最近使用」从当前 items 派生,同样不残留
 *
 * 恢复页面:从名单移除对应 name 即可,路由注册与 views 代码保持原样,无需动后端菜单权限。
 * 注意:隐藏是前端无条件拦截,与后端菜单权限相互独立(后端仍可正常返回该菜单 code)。
 */
export const HIDDEN_ROUTES: readonly string[] = [
  'package', // 套餐管理
  'orders', // 订单管理
  'alarm', // 费用告警
  'sso', // SSO配置
  'webhook', // Webhook配置
]

const hiddenSet = new Set(HIDDEN_ROUTES)

/** 路由 name 是否被前端隐藏 */
export function isRouteHidden(name: string): boolean {
  return hiddenSet.has(name)
}
