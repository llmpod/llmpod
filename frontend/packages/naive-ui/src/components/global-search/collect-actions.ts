import type { Component } from 'vue'
import type { GlobalSearchItem, SearchActionModule, SubTabModule } from './types'

export interface CollectSearchActionsOptions {
  /** 菜单权限判断(如 auth.hasMenu) */
  hasMenu: (name: string) => boolean
  /** 选中后跳转,一般是 (to) => void router.push(to) */
  navigate: (to: { name: string; query: { action: string } }) => void
  /** 结果分组名,默认「操作」 */
  group?: string
  /** 由路由 name 解析页面名,作为操作项的行尾小字(meta) */
  pageLabel?: (name: string) => string | undefined
}

/**
 * 聚合各页面 search.ts 声明的操作清单为全局搜索项。
 * 约定:文件路径形如 ../views/<路由name>/search.ts,目录名即路由 name,用于权限过滤与跳转;
 * search.ts 必须是纯数据文件,禁止 import 页面组件(eager glob 会把引用打进首包)。
 */
export function collectSearchActions(
  modules: Record<string, SearchActionModule>,
  options: CollectSearchActionsOptions,
): GlobalSearchItem[] {
  const { hasMenu, navigate, group = '操作', pageLabel } = options
  return Object.entries(modules).flatMap(([path, module]) => {
    const name = /\/views\/([^/]+)\/search\.ts$/.exec(path)?.[1]
    if (!name || !hasMenu(name)) return []
    return (module.searchActions ?? []).map((action) => ({
      key: `action:${name}:${action.action}`,
      label: action.label,
      group,
      meta: pageLabel?.(name),
      keywords: action.keywords,
      onSelect: () => navigate({ name, query: { action: action.action } }),
    }))
  })
}

export interface CollectSubTabsOptions {
  /** 父页面菜单权限判断(如 auth.hasMenu):子 tab 随父页面可见性过滤 */
  hasMenu: (name: string) => boolean
  /** 选中后跳转父页面并带 ?tab=,一般是 (to) => void router.push(to) */
  navigate: (to: { name: string; query: { tab: string } }) => void
  /** 父页面 → 所属分组(结果分组名与图标);未提供或父页面不在菜单则用默认 group 且无图标 */
  pageGroup?: (name: string) => { label: string; icon?: Component } | undefined
  /** 父页面 → 页面名,作为子 tab 的行尾小字(meta,标明归属页面);不传则子 tab 不显示归属 */
  pageLabel?: (name: string) => string | undefined
  /** 默认分组名,默认「页面」 */
  group?: string
}

/**
 * 聚合各页面 tabs.ts 声明的子 tab 为全局搜索项(页面导航类)。
 * 约定:文件路径形如 ../views/<路由name>/tabs.ts,目录名即父页面路由 name,用于权限过滤、
 * 分组归属与跳转;tabs.ts 必须是纯数据文件,禁止 import 页面组件(eager glob 会把引用打进首包)。
 * 默认 tab(进入页面即展示的首个 tab)无需声明——它等同于父页面本身,已有页面项覆盖。
 * 注意:目标页面需用 ?tab= 驱动 n-tabs 的 v-model,子 tab 才能被直达(参见约定文档)。
 */
export function collectSubTabs(
  modules: Record<string, SubTabModule>,
  options: CollectSubTabsOptions,
): GlobalSearchItem[] {
  const { hasMenu, navigate, pageGroup, pageLabel, group = '页面' } = options
  return Object.entries(modules).flatMap(([path, module]) => {
    const name = /\/views\/([^/]+)\/tabs\.ts$/.exec(path)?.[1]
    if (!name || !hasMenu(name)) return []
    const pg = pageGroup?.(name)
    return (module.searchTabs ?? []).map((tab) => ({
      key: `tab:${name}:${tab.tab}`,
      label: tab.label,
      group: pg?.label ?? group,
      icon: pg?.icon,
      meta: pageLabel?.(name),
      keywords: tab.keywords,
      onSelect: () => navigate({ name, query: { tab: tab.tab } }),
    }))
  })
}
