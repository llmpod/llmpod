import type { SubTab } from '@repo/naive-ui'

/**
 * 费用中心子 tab 全局搜索声明:每项作为独立页面项,点击直达本页对应 ?tab=。
 * 页面已不分 tab(余额与账户流水同页展示),暂无可直达的子 tab;
 * 恢复 tab 结构时取消下方注释即可。
 */
export const searchTabs: SubTab[] = [
  // { label: '账户流水', tab: 'consume' },
  // 暂时隐藏:套餐购买/账单管理/发票管理
  // { label: '套餐购买', tab: 'plan' },
  // { label: '账单管理', tab: 'bill' },
  // { label: '发票管理', tab: 'invoice' },
]
