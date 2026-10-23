import type { SubTab } from '@repo/naive-ui'

/**
 * 设置页子 tab 全局搜索声明:每项作为独立页面项,点击直达本页对应 ?tab=。
 * 「通用设置」虽为默认 tab,但父页面名为「设置」,仍声明以保留「通用设置」的搜索直达。
 * 「关于」已独立为左侧菜单页面,菜单页面项自动进全局搜索,无需在此声明。
 */
export const searchTabs: SubTab[] = [
  { label: '通用设置', tab: 'general', keywords: ['config', 'general'] },
  { label: '模块管理', tab: 'menu', keywords: ['menu', '菜单', '模块', '显示', '可见'] },
  { label: '品牌配置', tab: 'brand', keywords: ['brand', 'logo', '品牌'] },
]
