import type { SearchAction } from '@repo/naive-ui'

/** 全局搜索操作清单:action 对应本页 useEntryAction 的 handlers 键 */
export const searchActions: SearchAction[] = [
  { label: '添加模型', action: 'create', keywords: ['model'] },
]
