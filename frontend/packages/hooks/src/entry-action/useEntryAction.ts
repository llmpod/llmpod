import { watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

/** 入口动作:函数形式,或附带 clear(随 action 一并清除的一次性 query 键,如 ?id=) */
export type EntryActionHandler = (() => void) | { handler: () => void; clear?: string[] }

/**
 * 入口动作:URL 带 ?action=xxx 时执行一次对应动作(如自动打开新建弹窗),随后清除 query。
 * - 供全局搜索「操作」类结果的落地页使用;
 * - 用 watch 而非 onMounted:已停留在目标路由时 push 仅变更 query、组件不重挂载,watch 才能覆盖;
 * - 执行后立即 replace 移除 action 参数(保留其他 query),避免刷新/回退时重复触发;
 * - handler 形如 { handler, clear } 时,clear 声明的一次性参数(如 ?action=detail&id= 的 id)随 action 一并清除。
 */
export function useEntryAction(handlers: Record<string, EntryActionHandler>) {
  const route = useRoute()
  const router = useRouter()
  watch(
    () => route.query.action,
    (action) => {
      if (typeof action !== 'string') return
      const entry = handlers[action]
      if (!entry) return
      const { handler, clear } = typeof entry === 'function' ? { handler: entry } : entry
      // 只移除 action 与 clear 声明的一次性键,保留页面其他 query(如 tab、筛选状态)
      const query = { ...route.query }
      delete query.action
      for (const key of clear ?? []) delete query[key]
      void router.replace({ query })
      handler()
    },
    { immediate: true },
  )
}
