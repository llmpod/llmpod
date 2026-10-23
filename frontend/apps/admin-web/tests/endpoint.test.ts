// @vitest-environment happy-dom
import { h } from 'vue'
import type { VNode } from 'vue'
import { NSwitch } from 'naive-ui'
import type { DataTableColumn } from 'naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/endpoint', () => ({
  listEndpoints: vi.fn(),
  createEndpoint: vi.fn(),
  updateEndpoint: vi.fn(),
  deleteEndpoint: vi.fn(),
}))
vi.mock('@/views/model-release/components/endpoint/EndpointForm.vue', () => ({ default: {} }))

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
  listEndpoints,
  createEndpoint,
  updateEndpoint,
  deleteEndpoint,
  type EndpointData,
} from '@/api/endpoint'
import { useConfirm, useDialog, message } from '@repo/hooks'
import { useStore, type EndpointFormValues } from '@/views/model-release/components/endpoint/useStore'
import { useController } from '@/views/model-release/components/endpoint/useController'

function makeEndpoint(overrides: Partial<EndpointData> = {}): EndpointData {
  return {
    id: 1,
    deployment_id: null,
    instance_id: null,
    engine_type: 'external',
    protocol: 'openai',
    api_base: 'https://api.openai.com/v1',
    path: 'chat/completions',
    served_model: 'gpt-4o',
    name: 'OpenAI GPT-4o',
    provider_name: 'OpenAI',
    context_window: 128000,
    operations: ['chat.completions'],
    capabilities: [],
    enabled: true,
    health_status: 'healthy',
    last_synced_at: null,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  } as EndpointData
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listEndpoints).mockResolvedValue([
    makeEndpoint({ id: 1, name: 'ep-a', enabled: true }),
    makeEndpoint({ id: 2, name: 'ep-b', enabled: false, health_status: 'down' }),
  ])
  useTableMock.mockReturnValue({
    LmTable: {},
    LmPage: {},
    LmRefresh: {},
    LmColumn: {},
    LmSearch: {},
    refresh: vi.fn(),
    hasActiveQuery: { value: false },
    reset: vi.fn(),
  })
})

// ---------- store ----------

describe('endpoint useStore', () => {
  it('request 调 listEndpoints 全量返回,signal 透传', async () => {
    const controller = new AbortController()
    const all = await store.request({}, { signal: controller.signal })
    expect(listEndpoints).toHaveBeenCalledWith(controller.signal)
    expect(all.total).toBe(2)
  })

  it('create: 参数拼装(空值转 null) + notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(createEndpoint).mockResolvedValue({})

    const model: EndpointFormValues = {
      protocol: 'openai',
      api_base: 'https://api.openai.com/v1',
      served_model: 'gpt-4o',
      name: '新端点',
      api_key: '',
      provider_name: '',
      path: '',
      operations: [],
    }
    await store.create(model)
    expect(createEndpoint).toHaveBeenCalledWith({
      protocol: 'openai',
      api_base: 'https://api.openai.com/v1',
      served_model: 'gpt-4o',
      name: '新端点',
      api_key: null,
      provider_name: null,
      path: null,
      operations: null,
    })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('update: 调 updateEndpoint 并 notify;缺少 id 抛错', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateEndpoint).mockResolvedValue(makeEndpoint({ id: 1 }))

    await store.update({ id: 1, protocol: 'openai', api_base: 'https://x/v1', served_model: 'm', name: '改名', api_key: '', provider_name: '', path: '', operations: [] })
    expect(updateEndpoint).toHaveBeenCalledWith(1, expect.objectContaining({ name: '改名' }))
    expect(listener).toHaveBeenCalledTimes(1)

    await expect(
      store.update({ id: undefined as unknown as number, protocol: 'openai', api_base: '', served_model: '', name: '', api_key: '', provider_name: '', path: '', operations: [] }),
    ).rejects.toThrow('缺少端点 ID')
    unsubscribe()
  })

  it('toggleEnabled: 切换启用状态 + notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateEndpoint).mockResolvedValue(makeEndpoint({ enabled: false }))

    const next = await store.toggleEnabled(makeEndpoint({ id: 1, enabled: true }))
    expect(next).toBe(false)
    expect(updateEndpoint).toHaveBeenCalledWith(1, { enabled: false })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('remove: 调 deleteEndpoint 并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(deleteEndpoint).mockResolvedValue(undefined)

    await store.remove(1)
    expect(deleteEndpoint).toHaveBeenCalledWith(1)
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })
})

// ---------- controller ----------

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
    columns: Array<DataTableColumn<EndpointData>>
  }
}

/** 取指定 key 的列 render */
function columnRender(key: string): (row: EndpointData) => VNode {
  const col = tableOptions().columns.find(
    (c) => (c as unknown as Record<string, unknown>).key === key,
  ) as unknown as { render: (row: EndpointData) => VNode }
  expect(col).toBeTruthy()
  return col.render
}

/** 操作列 render 产物中的行操作项(真实 onClick 编排入口) */
function operateActions(): LmSelectAction<EndpointData>[] {
  const operate = tableOptions().columns.find(
    (col) => (col as unknown as Record<string, unknown>).__lmOperate === true,
  ) as unknown as { render: (row: EndpointData) => VNode } | undefined
  expect(operate).toBeTruthy()
  const vnode = operate!.render(makeEndpoint())
  return (vnode.props as { actions: LmSelectAction<EndpointData>[] }).actions
}

describe('endpoint useController', () => {
  it('openCreate 经 useDialog 打开创建弹窗', () => {
    const { controller, wrapper } = mountController()
    controller.openCreate()
    expect(useDialog).toHaveBeenCalledTimes(1)
    expect(vi.mocked(useDialog).mock.calls[0][0].title).toBe('接入端点')
    wrapper.unmount()
  })

  it('删除: 操作列驱动 useConfirm 确认后调接口并提示(全角冒号)', async () => {
    const { wrapper } = mountController()
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(deleteEndpoint).mockResolvedValue(undefined)

    const row = makeEndpoint({ id: 1, name: 'ep-a' })
    const removeAction = operateActions().find((action) => action.title === '删除')!
    await removeAction.onClick!(row)

    expect(useConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '删除端点' }))
    expect(deleteEndpoint).toHaveBeenCalledWith(1)
    expect(message.success).toHaveBeenCalledWith('已删除：ep-a')
    wrapper.unmount()
  })

  it('删除: useConfirm 取消时不调接口', async () => {
    const { wrapper } = mountController()
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))

    const removeAction = operateActions().find((action) => action.title === '删除')!
    await removeAction.onClick!(makeEndpoint({ id: 1, name: 'ep-a' }))

    expect(useConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '删除端点' }))
    expect(deleteEndpoint).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('启用列用 NSwitch 承接启停,value 绑定行状态', () => {
    const { wrapper } = mountController()
    const render = columnRender('enabled')

    const vnode = render(makeEndpoint({ id: 1, enabled: true }))
    expect(vnode.type).toBe(NSwitch)
    expect(vnode.props).toMatchObject({ value: true, size: 'small' })
    expect(render(makeEndpoint({ id: 2, enabled: false })).props).toMatchObject({ value: false })
    wrapper.unmount()
  })

  it('启用列 switch 停用:二次确认说明影响,确认后调 updateEndpoint 并提示,期间 loading 置位、结束复位', async () => {
    const { wrapper } = mountController()
    const render = columnRender('enabled')
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(updateEndpoint).mockResolvedValue(makeEndpoint({ enabled: false }))

    const row = makeEndpoint({ id: 1, name: 'ep-a', enabled: true })
    const toggle = (render(row).props as Record<string, unknown>)['onUpdate:value'] as () => Promise<void>
    const pending = toggle()
    expect((render(row).props as Record<string, unknown>).loading).toBe(true)

    await pending
    expect(useConfirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '停用端点',
        content: expect.stringContaining('这些模型将失去该上游，可能导致调用失败'),
      }),
    )
    expect(updateEndpoint).toHaveBeenCalledWith(1, { enabled: false })
    expect(message.success).toHaveBeenCalledWith('已停用：ep-a')
    expect((render(row).props as Record<string, unknown>).loading).toBe(false)
    wrapper.unmount()
  })

  it('启用列 switch 切换:useConfirm 取消时不调接口且 loading 复位', async () => {
    const { wrapper } = mountController()
    const render = columnRender('enabled')
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))

    const row = makeEndpoint({ id: 1, name: 'ep-a', enabled: false })
    const toggle = (render(row).props as Record<string, unknown>)['onUpdate:value'] as () => Promise<void>
    await toggle()

    expect(useConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '启用端点' }))
    expect(updateEndpoint).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
    expect((render(row).props as Record<string, unknown>).loading).toBe(false)
    wrapper.unmount()
  })

  it('操作列仅保留编辑/删除(启停已并入启用列 switch)', () => {
    const { wrapper } = mountController()
    const operate = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).__lmOperate === true,
    ) as unknown as { render: (row: EndpointData) => VNode } | undefined
    expect(operate).toBeTruthy()

    const vnode = operate!.render(makeEndpoint())
    const actions = (vnode.props as { actions: Array<{ title: string }> }).actions
    expect(actions.map((action) => action.title)).toEqual(['编辑', '删除'])
    wrapper.unmount()
  })
})