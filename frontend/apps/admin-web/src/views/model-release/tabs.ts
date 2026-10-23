import type { SubTab } from '@repo/naive-ui'

/**
 * 发布中心页子 tab 全局搜索声明:每项作为独立页面项,点击直达本页对应 ?tab=。
 * 「模型发布」虽为默认 tab,但父页面名为「发布中心」,仍声明以保留「模型发布」的搜索直达。
 */
export const searchTabs: SubTab[] = [
  { label: '模型发布', tab: 'release' },
  { label: '供应商管理', tab: 'provider', keywords: ['provider', 'vendor', 'endpoint', '端点'] },
]