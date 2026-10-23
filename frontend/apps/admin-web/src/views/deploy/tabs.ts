import type { SubTab } from '@repo/naive-ui'

/**
 * 模型管理页子 tab 全局搜索声明:每项作为独立页面项,点击直达本页对应 ?tab=。
 * 默认 tab「部署列表」仍声明——父页面名为「模型管理」,需保留「部署列表」的搜索直达。
 * 「模型发布」已独立为「发布中心」页(views/model-release),其子 tab 声明见该目录 tabs.ts。
 */
export const searchTabs: SubTab[] = [
  { label: '部署列表', tab: 'list' },
  { label: '模型管理', tab: 'management' },
]
