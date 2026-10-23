import type { Component } from 'vue'

/** 全局搜索项:页面导航或快捷操作 */
export interface GlobalSearchItem {
  /** 唯一标识(搜索索引缓存用) */
  key: string
  /** 显示名,如「节点列表」「新增租户」 */
  label: string
  /** 结果分组名,如「基础设施」「操作」 */
  group: string
  /** 行尾小字说明,如操作项的所属页面名(不展示则不传) */
  meta?: string
  /** 行首图标(函数式组件,如 BasicLayout 的 mask 图标) */
  icon?: Component
  /** 额外搜索关键词(英文别名/缩写等,不展示;中文拼音无需在此声明,自动匹配) */
  keywords?: string[]
  /** 选中后执行(路由跳转或触发操作),执行前面板自动关闭 */
  onSelect: () => void | Promise<void>
}

/** 页面在自身 search.ts 中声明的可搜索操作 */
export interface SearchAction {
  /** 显示名,如「新建用户」 */
  label: string
  /** 落地动作 key,对应页面 useEntryAction 的 handlers 键(当前均为 'create');同页内必须唯一(参与搜索项 key) */
  action: string
  /** 额外搜索关键词(英文别名/缩写等,不展示;中文拼音无需声明,自动匹配) */
  keywords?: string[]
}

/** import.meta.glob 收集到的 search.ts 模块形态 */
export interface SearchActionModule {
  searchActions?: SearchAction[]
}

/** 页面在自身 tabs.ts 中声明的可搜索子 tab(页面导航类) */
export interface SubTab {
  /** 显示名,如「部署列表」「模型管理」 */
  label: string
  /** 子 tab 标识,对应页面 n-tab-pane 的 name,选中时作为 ?tab= 透传到本页 */
  tab: string
  /** 额外搜索关键词(英文别名/缩写等,不展示;中文拼音无需声明,自动匹配) */
  keywords?: string[]
}

/** import.meta.glob 收集到的 tabs.ts 模块形态 */
export interface SubTabModule {
  searchTabs?: SubTab[]
}
