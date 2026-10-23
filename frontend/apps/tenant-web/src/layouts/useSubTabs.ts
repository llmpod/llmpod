import type { Component } from 'vue'
import { collectSubTabs } from '@repo/naive-ui'
import type { GlobalSearchItem, SubTabModule } from '@repo/naive-ui'
import { useAuthStore } from '@/store/auth'

/** 各页面目录下的子 tab 清单(约定:views/<路由name>/tabs.ts,纯数据,勿引入页面组件) */
const modules = import.meta.glob<SubTabModule>('../views/*/tabs.ts', { eager: true })

/** 全局搜索「子 tab」页面项:聚合所有页面 tabs.ts,按父页面菜单权限过滤;新增/增删子 tab 改 tabs.ts 即自动生效 */
export function useSubTabs(
  pageGroup?: (name: string) => { label: string; icon?: Component } | undefined,
  pageLabel?: (name: string) => string | undefined,
) {
  const auth = useAuthStore()
  const router = useRouter()
  return computed<GlobalSearchItem[]>(() =>
    collectSubTabs(modules, {
      hasMenu: (name) => auth.hasMenu(name),
      navigate: (to) => void router.push(to),
      pageGroup,
      pageLabel,
    }),
  )
}
