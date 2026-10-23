// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/publish', () => ({
  listPublishes: vi.fn(),
  listMarketModels: vi.fn(),
  createRoute: vi.fn(),
  unpublishRoute: vi.fn(),
  republishRoute: vi.fn(),
  updateRoute: vi.fn(),
  deleteRoute: vi.fn(),
  upsertBinding: vi.fn(),
  removeBinding: vi.fn(),
  updateBinding: vi.fn(),
  disableBinding: vi.fn(),
  enableBinding: vi.fn(),
}))
vi.mock('@/views/model-release/components/RouteForm.vue', () => ({ default: {} }))
vi.mock('@/views/model-release/components/BindingManage.vue', () => ({ default: {} }))
// useTable 替换为桩以捕获列定义;useOperate 保留真实现(操作列 render 可断言)
const useTableMock = vi.fn()

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useTable: (...args: unknown[]) => useTableMock(...args),
    useDialog: vi.fn(),
    useConfirm: vi.fn(),
    useEntryAction: vi.fn(),
    message: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
  }
})

import {
  listPublishes,
  unpublishRoute,
  republishRoute,
  deleteRoute,
  type PublishData,
  type RouteData,
} from '@/api/publish'
import { useConfirm, message } from '@repo/hooks'
import type { LmSelectAction } from '@repo/naive-ui'
import { useStore } from '@/views/model-release/useStore'
import { useController } from '@/views/model-release/useController'

function makeRoute(overrides: Partial<RouteData> = {}): RouteData {
  return {
    id: 1,
    name: 'gpt-4o',
    status: 'published',
    origin: 'manual',
    model_type: 'llm',
    description: null,
    context_length: null,
    tags: [],
    input_price_per_mtok: '2.5',
    output_price_per_mtok: '10',
    cached_input_price_per_mtok: null,
    failover_enabled: true,
    failover_max_retries: 2,
    created_at: '',
    updated_at: '',
    ...overrides,
  }
}

function makePublish(overrides: Partial<PublishData> = {}): PublishData {
  return { route: makeRoute(), bindings: [], ...overrides }
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listPublishes).mockResolvedValue([
    makePublish({ route: makeRoute({ id: 1, name: 'gpt-4o' }) }),
    makePublish({ route: makeRoute({ id: 2, name: 'claude-3', status: 'unpublished' }) }),
  ])
  useTableMock.mockReturnValue({
    LmTable: {},
    LmPage: {},
    LmRefresh: {},
    LmColumn: {},
    LmSearch: {},
    param: { keyword: '' },
    search: vi.fn(),
    refresh: vi.fn(),
    hasActiveQuery: { value: false },
    reset: vi.fn(),
  })
})

// ---------- store: 参数拼装 / 数据映射 / 错误分支 / 副作用 ----------

describe('model-release useStore', () => {
  it('request 调 listPublishes 全量返回,keyword 前端过滤,signal 透传,前端分页切片', async () => {
    const controller = new AbortController()
    const all = await store.request({}, { signal: controller.signal })
    expect(listPublishes).toHaveBeenCalledWith(controller.signal)
    expect(all.total).toBe(2)
    expect(all.data).toHaveLength(2)

    const filtered = await store.request({ keyword: 'GPT' })
    expect(filtered.total).toBe(1)
    expect(filtered.data[0].route.name).toBe('gpt-4o')

    const paged = await store.request({ page: 2, pageSize: 1 })
    expect(paged.total).toBe(2)
    expect(paged.data).toHaveLength(1)
    expect(paged.data[0].route.id).toBe(2)
  })

  it('unpublish/republish/remove: 分别调对应接口并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(unpublishRoute).mockResolvedValue(makeRoute({ status: 'unpublished' }))
    vi.mocked(republishRoute).mockResolvedValue(makeRoute({ status: 'published' }))
    vi.mocked(deleteRoute).mockResolvedValue(null)

    await store.unpublish(1)
    expect(unpublishRoute).toHaveBeenCalledWith(1)
    await store.republish(1)
    expect(republishRoute).toHaveBeenCalledWith(1)
    await store.remove(1)
    expect(deleteRoute).toHaveBeenCalledWith(1)
    expect(listener).toHaveBeenCalledTimes(3)
    unsubscribe()
  })

  it('remove: 接口失败时抛错且不 notify(HTTP 层统一提示)', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(deleteRoute).mockRejectedValue(new Error('boom'))

    await expect(store.remove(1)).rejects.toThrow('boom')
    expect(listener).not.toHaveBeenCalled()
    unsubscribe()
  })
})

// ---------- controller: 编排 ----------

function mountController() {
  let controller!: ReturnType<typeof useController>
  const wrapper = mount({
    setup: () => {
      controller = useController()
      return () => h('div')
    },
  })
  return { controller, wrapper }
}

/** useTable 收到的 options(列定义断言入口) */
function tableOptions() {
  expect(useTableMock).toHaveBeenCalledTimes(1)
  return useTableMock.mock.calls[0][0] as {
    columns: Array<Record<string, unknown>>
  }
}

/** 操作列 render 产物中的行操作项(真实 onClick 编排入口) */
function operateActions(): LmSelectAction<PublishData>[] {
  const operate = tableOptions().columns.find((col) => col.__lmOperate === true) as unknown as {
    render: (row: PublishData) => { props: { actions: LmSelectAction<PublishData>[] } }
  }
  expect(operate).toBeTruthy()
  return operate.render(makePublish()).props.actions
}

describe('model-release useController', () => {
  it('useTable 以 store 数据源装配,request 调 listPublishes 并前端切片', async () => {
    const { wrapper } = mountController()
    const options = useTableMock.mock.calls[0][0] as {
      request: (params: { page?: number; pageSize?: number }) => Promise<{ total: number; data: PublishData[] }>
    }
    const result = await options.request({ page: 2, pageSize: 1 })
    expect(listPublishes).toHaveBeenCalled()
    expect(result.total).toBe(2)
    expect(result.data[0].route.id).toBe(2)
    wrapper.unmount()
  })

  it('onRemove: 操作列驱动,useConfirm 确认后调 deleteRoute + 成功提示(全角冒号)', async () => {
    const { wrapper } = mountController()
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(deleteRoute).mockResolvedValue(null)

    const removeAction = operateActions().find((action) => action.title === '删除')!
    await removeAction.onClick!(makePublish({ route: makeRoute({ id: 1, name: 'gpt-4o' }) }))

    expect(useConfirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '删除路由',
        content: '确定删除「gpt-4o」吗？将连带清除成员绑定与租户授权，不可恢复。',
      }),
    )
    expect(deleteRoute).toHaveBeenCalledWith(1)
    expect(message.success).toHaveBeenCalledWith('已删除：gpt-4o')
    wrapper.unmount()
  })

  it('onRemove: useConfirm 取消时不调接口', async () => {
    const { wrapper } = mountController()
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))

    const removeAction = operateActions().find((action) => action.title === '删除')!
    await removeAction.onClick!(makePublish({ route: makeRoute({ id: 1, name: 'gpt-4o' }) }))

    expect(useConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '删除路由' }))
    expect(deleteRoute).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
