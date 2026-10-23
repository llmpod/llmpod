import { collectSearchActions } from '@repo/naive-ui'
import type { GlobalSearchItem, SearchActionModule } from '@repo/naive-ui'
import { useAuthStore } from '@/store/auth'

/** 各页面目录下的搜索操作清单(约定:views/<路由name>/search.ts,纯数据,勿引入页面组件) */
const modules = import.meta.glob<SearchActionModule>('../views/*/search.ts', { eager: true })

/** 全局搜索「操作」分组:聚合所有页面 search.ts,按目标页面菜单权限过滤;新增页面放 search.ts 即自动生效 */
export function useSearchActions(pageLabel?: (name: string) => string | undefined) {
  const auth = useAuthStore()
  const router = useRouter()
  return computed<GlobalSearchItem[]>(() =>
    collectSearchActions(modules, {
      hasMenu: (name) => auth.hasMenu(name),
      navigate: (to) => void router.push(to),
      pageLabel,
    }),
  )
}
