import type { SearchAction } from '@repo/naive-ui'

/** 全局搜索操作清单:action 对应本页 useEntryAction 的 handlers 键 */
export const searchActions: SearchAction[] = [
  { label: '注册节点', action: 'create', keywords: ['node', 'register'] },
]
