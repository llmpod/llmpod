// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  // route 必须响应式:useEntryAction 的 watch 依赖 route.query.action 的深度追踪
  const route = reactive({ query: {} as Record<string, unknown> })
  const router = { replace: vi.fn() }
  return {
    useRoute: () => route,
    useRouter: () => router,
    __route: route,
    __router: router,
  }
})

import * as vueRouterMock from 'vue-router'
import { useEntryAction, type EntryActionHandler } from '../src'

const route = (vueRouterMock as unknown as { __route: { query: Record<string, unknown> } }).__route
const router = (vueRouterMock as unknown as { __router: { replace: ReturnType<typeof vi.fn> } }).__router

/** 在组件 setup 内调用(useRoute 需要注入上下文) */
function setupEntryAction(handlers: Record<string, EntryActionHandler>) {
  return mount({
    setup: () => {
      useEntryAction(handlers)
      return () => h('div')
    },
  })
}

afterEach(() => {
  route.query = {}
  router.replace.mockClear()
})

describe('useEntryAction 入口动作', () => {
  it('初始 URL 带 ?action 时立即执行对应动作并清除 query', () => {
    route.query = { action: 'create', tab: 'list' }
    const create = vi.fn()
    setupEntryAction({ create })

    expect(create).toHaveBeenCalledTimes(1)
    // 仅移除 action,保留 tab 等其他 query
    expect(router.replace).toHaveBeenCalledWith({ query: { tab: 'list' } })
  })

  it('已停留目标路由时 query 变更也能触发(watch 而非 onMounted)', async () => {
    route.query = {}
    const create = vi.fn()
    setupEntryAction({ create })
    expect(create).not.toHaveBeenCalled()

    route.query = { action: 'create' }
    await Promise.resolve() // watch 回调微任务
    expect(create).toHaveBeenCalledTimes(1)
    expect(router.replace).toHaveBeenCalledWith({ query: {} })
  })

  it('action 非字符串(数组)时忽略', () => {
    const create = vi.fn()
    route.query = { action: ['create', 'create'] }
    setupEntryAction({ create })
    expect(create).not.toHaveBeenCalled()
    expect(router.replace).not.toHaveBeenCalled()
  })

  it('未知 action 不执行也不清 query', () => {
    route.query = { action: 'unknown-op', tab: 'x' }
    const create = vi.fn()
    setupEntryAction({ create })
    expect(create).not.toHaveBeenCalled()
    expect(router.replace).not.toHaveBeenCalled()
  })

  it('handler 声明 clear 时,一次性参数随 action 一并清除', () => {
    route.query = { action: 'detail', id: 'd1', tab: 'list' }
    const detail = vi.fn()
    setupEntryAction({ detail: { handler: detail, clear: ['id'] } })

    expect(detail).toHaveBeenCalledTimes(1)
    // action 与 clear 声明的 id 一并移除,保留 tab
    expect(router.replace).toHaveBeenCalledWith({ query: { tab: 'list' } })
  })

  it('handler 对象形式缺省 clear 时仅移除 action', () => {
    route.query = { action: 'create', tab: 'list', id: 'd1' }
    const create = vi.fn()
    setupEntryAction({ create: { handler: create } })

    expect(create).toHaveBeenCalledTimes(1)
    expect(router.replace).toHaveBeenCalledWith({ query: { tab: 'list', id: 'd1' } })
  })
})
