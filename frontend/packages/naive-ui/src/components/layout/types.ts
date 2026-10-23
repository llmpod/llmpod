import type { Component } from 'vue'

/** 布局菜单项:name 即路由 name,点击 router.push({ name }),同时作为高亮 key */
export interface LayoutMenuItem {
  label: string
  name: string
  icon?: Component
  /** 有权限但被可见性开关下线:置灰展示且禁止跳转(由 /auth/menus 的 enabled 驱动) */
  disabled?: boolean
}

/** 布局菜单分组:有 children 时渲染为可折叠子菜单;省略 children 时渲染为可点击的叶子菜单(用 name 导航) */
export interface LayoutMenuGroup {
  label: string
  icon?: Component
  /** 分组菜单节点 key;缺省时按菜单顺序取 `group-{index}`,菜单配置不稳定(如按权限过滤)时应显式指定 */
  key?: string
  /** 叶子菜单的路由 name,点击 router.push({ name }) 并高亮;仅在无 children 时使用 */
  name?: string
  /** 子菜单项;省略时本分组作为叶子菜单项直接展示 */
  children?: LayoutMenuItem[]
  /** 初始是否展开,默认 true;仅对有 children 的分组生效 */
  defaultExpanded?: boolean
  /** 叶子分组有权限但被可见性开关下线:置灰展示且禁止跳转 */
  disabled?: boolean
}
