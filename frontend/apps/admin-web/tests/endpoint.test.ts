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
  createEndpoints: vi.fn(),
  fetchUpstreamModels: vi.fn(),
  testEndpoint: vi.fn(),
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
  createEndpoints,
  fetchUpstreamModels,
  testEndpoint,
  updateEndpoint,
  deleteEndpoint,
  type EndpointData,
} from '@/api/endpoint'
import { useConfirm, useDialog, message } from '@repo/hooks'
import {
  useStore,
  normalizeModels,
  createModelItem,
  type EndpointFormValues,
} from '@/views/model-release/components/endpoint/useStore'
import { useController } from '@/views/model-release/components/endpoint/useController'
import EndpointModelRows from '@/views/model-release/components/endpoint/EndpointModelRows.vue'

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
    upstream_type: 'external',
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

  it('create: 行数据归并进 models 数组(去空白/去重/丢空行),显示名留空不下发 name + notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(createEndpoints).mockResolvedValue([
      makeEndpoint({ id: 1, name: 'gpt-4o' }),
      makeEndpoint({ id: 2, name: 'gpt-4o-mini' }),
    ])

    const model: EndpointFormValues = {
      protocol: 'openai',
      api_base: 'https://api.openai.com/v1',
      models: [
        createModelItem(' gpt-4o '),
        createModelItem('gpt-4o-mini', ' '),
        createModelItem('gpt-4o'),
        createModelItem('  '),
      ],
      served_model: '',
      name: '',
      api_key: '',
      provider_name: '',
      path: '',
      operations: [],
    }
    const created = await store.create(model)
    expect(createEndpoints).toHaveBeenCalledWith({
      protocol: 'openai',
      api_base: 'https://api.openai.com/v1',
      api_key: null,
      provider_name: null,
      path: null,
      models: [{ served_model: 'gpt-4o' }, { served_model: 'gpt-4o-mini' }],
    })
    // 显示名留空时不下发该键(空串会整批 422),缺省取 served_model 由后端负责
    expect(Object.keys(createEndpoints.mock.calls[0][0].models[0])).toEqual(['served_model'])
    expect(created.map((item) => item.name)).toEqual(['gpt-4o', 'gpt-4o-mini'])
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('create: 行上填了显示名则随该行下发 name', async () => {
    vi.mocked(createEndpoints).mockResolvedValue([makeEndpoint({ id: 1, name: 'GPT-4o' })])

    await store.create({
      protocol: 'openai',
      api_base: 'https://api.openai.com/v1',
      models: [createModelItem(' gpt-4o ', ' GPT-4o ')],
      served_model: '',
      name: '',
      api_key: '',
      provider_name: '',
      path: '',
      operations: [],
    })

    expect(createEndpoints.mock.calls[0][0].models).toEqual([
      { served_model: 'gpt-4o', name: 'GPT-4o' },
    ])
  })

  it('normalizeModels: 模型 ID 去空白/去重/丢空行,显示名空白不带 name 键,不按逗号拆分', () => {
    expect(
      normalizeModels([createModelItem(' a '), createModelItem('b'), createModelItem('a')]),
    ).toEqual([{ served_model: 'a' }, { served_model: 'b' }])
    // 逐行输入:含逗号的模型名整体保留,不拆
    expect(normalizeModels([createModelItem('a,b')])).toEqual([{ served_model: 'a,b' }])
    expect(normalizeModels([createModelItem('m', '  ')])).toEqual([{ served_model: 'm' }])
    expect(normalizeModels([createModelItem('m', ' 别名 ')])).toEqual([
      { served_model: 'm', name: '别名' },
    ])
    expect(normalizeModels([])).toEqual([])
    expect(normalizeModels(undefined)).toEqual([])
    expect(normalizeModels([createModelItem('  '), createModelItem('', ' 别名 ')])).toEqual([])
  })

  it('fetchUpstreamModels: 透传入参并返回上游清单(不落库、不 notify)', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(fetchUpstreamModels).mockResolvedValue(['gpt-4o', 'o1'])

    const ids = await store.fetchUpstreamModels({
      protocol: 'anthropic',
      api_base: 'https://api.anthropic.com/v1',
      api_key: null,
    })

    expect(fetchUpstreamModels).toHaveBeenCalledWith({
      protocol: 'anthropic',
      api_base: 'https://api.anthropic.com/v1',
      api_key: null,
    })
    expect(ids).toEqual(['gpt-4o', 'o1'])
    expect(listener).not.toHaveBeenCalled()
    unsubscribe()
  })

  it('update: 调 updateEndpoint 并 notify;缺少 id 抛错', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateEndpoint).mockResolvedValue(makeEndpoint({ id: 1 }))

    await store.update({ id: 1, protocol: 'openai', api_base: 'https://x/v1', models: [], served_model: 'm', name: '改名', api_key: '', provider_name: '', path: '', operations: [] })
    expect(updateEndpoint).toHaveBeenCalledWith(1, expect.objectContaining({ name: '改名' }))
    expect(listener).toHaveBeenCalledTimes(1)

    await expect(
      store.update({ id: undefined as unknown as number, protocol: 'openai', api_base: '', models: [], served_model: '', name: '', api_key: '', provider_name: '', path: '', operations: [] }),
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

  it('testConnection: 按行 ID 探测已存配置 + notify(后端写回健康,列表需跟着刷新)', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(testEndpoint).mockResolvedValue({
      endpoint_id: 7,
      healthy: true,
      url: 'https://api.openai.com/v1/chat/completions',
      status_code: 200,
      detail: null,
      elapsed_ms: 320,
    })

    const result = await store.testConnection(7)
    // 只传 endpoint_id:其余字段由后端回落该行存储值,探的才是行现状
    expect(testEndpoint).toHaveBeenCalledWith({ endpoint_id: 7 })
    expect(result.healthy).toBe(true)
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

  it('供应商列:供应商为空时按 upstream_type 兜底(本地部署=本地/外部登记=外部),有值用供应商名', () => {
    const { wrapper } = mountController()
    const render = columnRender('provider_name') as unknown as (row: EndpointData) => string

    expect(render(makeEndpoint({ provider_name: 'OpenAI', upstream_type: 'external' }))).toBe('OpenAI')
    expect(render(makeEndpoint({ provider_name: null, upstream_type: 'local' }))).toBe('本地')
    expect(render(makeEndpoint({ provider_name: null, upstream_type: 'external' }))).toBe('外部')
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

  it('测试: 操作列点击直接探测,连通正常提示耗时(无二次确认)', async () => {
    const { wrapper } = mountController()
    vi.mocked(testEndpoint).mockResolvedValue({
      endpoint_id: 1,
      healthy: true,
      url: 'https://api.openai.com/v1/chat/completions',
      status_code: 200,
      detail: null,
      elapsed_ms: 320,
    })

    const row = makeEndpoint({ id: 1, name: 'ep-a' })
    const testAction = operateActions().find((action) => action.title === '测试')!
    // onClick 返回 Promise:LmTableSelect 据此把按钮转 loading 并拦连点
    const pending = testAction.onClick!(row)
    expect(pending).toBeInstanceOf(Promise)
    await pending

    // 点击即测,不走二次确认
    expect(useConfirm).not.toHaveBeenCalled()
    expect(testEndpoint).toHaveBeenCalledWith({ endpoint_id: 1 })
    expect(message.success).toHaveBeenCalledWith('连通正常：ep-a · 耗时 320ms')
    expect(message.error).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('测试: 上游拒绝时提示 HTTP 状态 + 上游原话,不当成接口异常', async () => {
    const { wrapper } = mountController()
    vi.mocked(testEndpoint).mockResolvedValue({
      endpoint_id: 1,
      healthy: false,
      url: 'https://api.openai.com/v1/chat/completions',
      status_code: 401,
      detail: '{"error":{"message":"Incorrect API key provided"}}',
      elapsed_ms: 180,
    })

    const testAction = operateActions().find((action) => action.title === '测试')!
    await testAction.onClick!(makeEndpoint({ id: 1, name: 'ep-a' }))

    expect(message.success).not.toHaveBeenCalled()
    expect(message.error).toHaveBeenCalledWith(
      '连通异常：ep-a · HTTP 401 · 耗时 180ms\n{"error":{"message":"Incorrect API key provided"}}',
    )
    wrapper.unmount()
  })

  it('测试: 连接失败/超时无状态码时按「连接失败」文案提示', async () => {
    const { wrapper } = mountController()
    vi.mocked(testEndpoint).mockResolvedValue({
      endpoint_id: 1,
      healthy: false,
      url: 'https://api.openai.com/v1/chat/completions',
      status_code: null,
      detail: 'ConnectError: [Errno 111] Connection refused',
      elapsed_ms: 5000,
    })

    const testAction = operateActions().find((action) => action.title === '测试')!
    await testAction.onClick!(makeEndpoint({ id: 1, name: 'ep-a' }))

    expect(message.error).toHaveBeenCalledWith(
      '连通异常：ep-a · 连接失败/超时 · 耗时 5000ms\nConnectError: [Errno 111] Connection refused',
    )
    wrapper.unmount()
  })

  it('测试: 探测真错误(422/404)由 HTTP 层提示,控制器不重复弹提示', async () => {
    const { wrapper } = mountController()
    vi.mocked(testEndpoint).mockRejectedValue(new Error('本地部署端点不适用'))

    const testAction = operateActions().find((action) => action.title === '测试')!
    await testAction.onClick!(makeEndpoint({ id: 1, name: 'ep-a' }))

    expect(message.success).not.toHaveBeenCalled()
    expect(message.error).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('测试: 本地部署端点不展示该项(后端判不适用),外部端点展示', () => {
    const { wrapper } = mountController()
    const testAction = operateActions().find((action) => action.title === '测试')!
    const show = testAction.show as (row: EndpointData) => boolean

    expect(show(makeEndpoint({ upstream_type: 'external' }))).toBe(true)
    expect(show(makeEndpoint({ upstream_type: 'local' }))).toBe(false)
    wrapper.unmount()
  })

  it('操作列保留测试/编辑/删除(启停已并入启用列 switch)', () => {
    const { wrapper } = mountController()
    const operate = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).__lmOperate === true,
    ) as unknown as { render: (row: EndpointData) => VNode } | undefined
    expect(operate).toBeTruthy()

    const vnode = operate!.render(makeEndpoint())
    const actions = (vnode.props as { actions: Array<{ title: string }> }).actions
    expect(actions.map((action) => action.title)).toEqual(['测试', '编辑', '删除'])
    wrapper.unmount()
  })
})

// ---------- 模型行编辑器 ----------

describe('EndpointModelRows', () => {
  /** 最近一次 update:value 的行数据 */
  function lastRows(wrapper: ReturnType<typeof mount>): unknown {
    const events = wrapper.emitted('update:value')!
    return events[events.length - 1][0]
  }

  /** 按文字找按钮(「添加模型」在列表末尾,删除按钮带图标无文字) */
  function buttonByText(wrapper: ReturnType<typeof mount>, text: string) {
    const btn = wrapper.findAll('button').find((item) => item.text().includes(text))
    expect(btn).toBeTruthy()
    return btn!
  }

  it('点「添加模型」追加一空行', async () => {
    const wrapper = mount(EndpointModelRows, {
      props: { value: [createModelItem('gpt-4o', 'GPT-4o')] },
    })

    await buttonByText(wrapper, '添加模型').trigger('click')

    expect(lastRows(wrapper)).toEqual([
      { served_model: 'gpt-4o', name: 'GPT-4o' },
      { served_model: '', name: '' },
    ])
    wrapper.unmount()
  })

  it('删行:多行时移除该行,删到零行时补一空行', async () => {
    const wrapper = mount(EndpointModelRows, {
      props: { value: [createModelItem('a'), createModelItem('b')] },
    })
    await wrapper.findAll('button')[0].trigger('click')
    expect(lastRows(wrapper)).toEqual([{ served_model: 'b', name: '' }])

    const single = mount(EndpointModelRows, { props: { value: [createModelItem('a')] } })
    await single.findAll('button')[0].trigger('click')
    expect(lastRows(single)).toEqual([{ served_model: '', name: '' }])
    wrapper.unmount()
    single.unmount()
  })

  it('改模型 ID / 显示名称:按行回写且不动其它行', async () => {
    const value = [createModelItem('a', 'A'), createModelItem('b', 'B')]

    // 行内字段顺序:模型 ID(auto-complete) → 显示名称(input),第二行显示名下标 3
    const idWrapper = mount(EndpointModelRows, { props: { value } })
    await idWrapper.findAll('input')[0].setValue('a1')
    expect(lastRows(idWrapper)).toEqual([
      { served_model: 'a1', name: 'A' },
      { served_model: 'b', name: 'B' },
    ])
    idWrapper.unmount()

    const nameWrapper = mount(EndpointModelRows, { props: { value } })
    await nameWrapper.findAll('input')[3].setValue('B1')
    expect(lastRows(nameWrapper)).toEqual([
      { served_model: 'a', name: 'A' },
      { served_model: 'b', name: 'B1' },
    ])
    nameWrapper.unmount()
  })

  it('点模型 ID 输入框触发上游清单拉取(fetch)', async () => {
    const wrapper = mount(EndpointModelRows, { props: { value: [createModelItem()] } })

    await wrapper.findAll('input')[0].trigger('mousedown')

    expect(wrapper.emitted('fetch')).toHaveLength(1)
    wrapper.unmount()
  })

  it('拉取失败时给出「直接手填」引导', () => {
    const wrapper = mount(EndpointModelRows, {
      props: { value: [createModelItem()], failed: true },
    })
    expect(wrapper.text()).toContain('未取到上游模型清单')
    wrapper.unmount()
  })

  it('候选按当前行输入做包含匹配', () => {
    const wrapper = mount(EndpointModelRows, {
      props: {
        value: [createModelItem('gpt')],
        options: [
          { label: 'gpt-4o', value: 'gpt-4o' },
          { label: 'o1-mini', value: 'o1-mini' },
        ],
      },
    })
    // 组件自己算过滤结果后透传给 n-auto-complete(n-auto-complete 不自带包含匹配)
    const autocomplete = wrapper.findComponent({ name: 'AutoComplete' })
    const options = (autocomplete.props() as { options: { label: string }[] }).options
    expect(options.map((option) => option.label)).toEqual(['gpt-4o'])
    wrapper.unmount()
  })

  it('输入框为空时也弹候选:getShow 恒真 + 交出全部未占用候选', () => {
    const wrapper = mount(EndpointModelRows, {
      props: {
        value: [createModelItem()],
        options: [
          { label: 'gpt-4o', value: 'gpt-4o' },
          { label: 'o1-mini', value: 'o1-mini' },
        ],
      },
    })
    const autocomplete = wrapper.findComponent({ name: 'AutoComplete' })
    const props = autocomplete.props() as {
      getShow: (value: string) => boolean
      options: { label: string }[]
    }
    // naive-ui 缺省 getShow 是 !!value,空输入时菜单不弹;这里必须恒真
    expect(props.getShow('')).toBe(true)
    expect(props.options.map((option) => option.label)).toEqual(['gpt-4o', 'o1-mini'])
    wrapper.unmount()
  })

  it('候选剔除已被其它行占用的模型名(同批重复会被后端整批 422)', () => {
    const wrapper = mount(EndpointModelRows, {
      props: {
        value: [createModelItem('gpt-4o'), createModelItem()],
        options: [
          { label: 'gpt-4o', value: 'gpt-4o' },
          { label: 'o1-mini', value: 'o1-mini' },
        ],
      },
    })
    const rows = wrapper.findAllComponents({ name: 'AutoComplete' })
    const first = rows[0].props() as { options: { label: string }[] }
    const second = rows[1].props() as { options: { label: string }[] }
    // 第一行自己填了 gpt-4o:按关键字匹配后只剩它,自己占用的不算冲突
    expect(first.options.map((option) => option.label)).toEqual(['gpt-4o'])
    // 第二行空输入:全量候选里剔除已被第一行占用的 gpt-4o
    expect(second.options.map((option) => option.label)).toEqual(['o1-mini'])
    wrapper.unmount()
  })

  it('候选菜单给了宽度下限,长模型名不被截断', () => {
    const wrapper = mount(EndpointModelRows, { props: { value: [createModelItem()] } })
    const props = wrapper.findComponent({ name: 'AutoComplete' }).props() as {
      menuProps: { style: { minWidth: string } }
    }
    // naive-ui 内部写死 width: target,不设下限时长名会截成 "Qwen3.8-27B-AWQ-…"
    expect(props.menuProps.style.minWidth).toBe('24rem')
    wrapper.unmount()
  })

  it('聚焦模型 ID 也触发拉清单(focus 事件)', async () => {
    const wrapper = mount(EndpointModelRows, { props: { value: [createModelItem()] } })

    await wrapper.findComponent({ name: 'AutoComplete' }).vm.$emit('focus')

    expect(wrapper.emitted('fetch')).toHaveLength(1)
    wrapper.unmount()
  })
})