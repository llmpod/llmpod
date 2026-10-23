import type { SearchAction } from '@repo/naive-ui'

/** 全局搜索操作清单:action 对应本页 useEntryAction 的 handlers 键 */
export const searchActions: SearchAction[] = [
  { label: '新建部署', action: 'create', keywords: ['deploy'] },
  { label: '部署向导', action: 'wizard', keywords: ['wizard', 'quick'] },
]
