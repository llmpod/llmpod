// @vitest-environment happy-dom
import { h } from 'vue'
import type { VNode } from 'vue'
import { mount } from '@vue/test-utils'
import { NEllipsis, NTag } from 'naive-ui'
import type { DataTableColumn } from 'naive-ui'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/request-log', () => ({
  listRequestLogs: vi.fn(),
  getRequestLog: vi.fn(),
}))
vi.mock('@/api/tenant', () => ({ listTenants: vi.fn() }))
vi.mock('@/views/bills/RequestLogDetail.vue', () => ({ default: {} }))
// useTable 保留真实现(既有用例依赖 search/reset 行为),仅捕获 options 以断言列定义
const useTableMock = vi.fn()

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useTable: (...args: unknown[]) => {
      useTableMock(...args)
      return (actual.useTable as (...rest: unknown[]) => unknown)(...args)
    },
    useDialog: vi.fn(),
    useConfirm: vi.fn(),
    useEntryAction: vi.fn(),
    message: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
  }
})

import {
  listRequestLogs,
  getRequestLog,
  type ListRequestLogsParams,
  type RequestLogData,
  type RequestLogDetailData,
} from '@/api/request-log'
import { listTenants } from '@/api/tenant'
import { useDialog } from '@repo/hooks'
import {
  formatDateTime,
  formatDecimal,
  logStatusMap,
  statusFilterOptions,
  upstreamTypeConf,
  useStore,
} from '@/views/bills/useStore'
import { useController } from '@/views/bills/useController'

function makeLog(overrides: Partial<RequestLogData> = {}): RequestLogData {
  return {
    id: 1,
    request_id: 'req-1',
    tenant_id: 3,
    api_key_id: 1,
    model_alias: 'gpt-4o',
    served_model: 'gpt-4o-2024-08-06',
    operation: 'chat.completions',
    protocol: 'openai',
    engine: 'vllm',
    upstream_type: 'local',
    provider_name: null,
    endpoint_id: null,
    retry_count: 0,
    prompt_tokens: 100,
    completion_tokens: 50,
    total_tokens: 150,
    cached_tokens: 20,
    cost: '0.012500000',
    latency_ms: 300,
    status: 'success',
    http_status: 200,
    request_ref: 'req-ref',
    response_ref: 'res-ref',
    conv_prefix_hash: 'hash-1',
    truncated: false,
    snapshot_disabled: false,
    error_message: null,
    error_detail: null,
    created_at: '2025-03-03T04:05:00Z',
    ...overrides,
  } as RequestLogData
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listRequestLogs).mockResolvedValue({
    items: [makeLog({ id: 1 }), makeLog({ id: 2, request_id: 'req-2', status: 'error' })],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  })
  vi.mocked(listTenants).mockResolvedValue({
    items: [
      { id: 0, name: '平台租户', status: 'active', created_at: '', updated_at: '', user_count: 0, role_count: 0, balance: '0', frozen_balance: '0', available: '0', blocked_models: [] },
      { id: 3, name: '企业A', status: 'active', created_at: '', updated_at: '', user_count: 0, role_count: 0, balance: '0', frozen_balance: '0', available: '0', blocked_models: [] },
      { id: 7, name: '企业B', status: 'active', created_at: '', updated_at: '', user_count: 0, role_count: 0, balance: '0', frozen_balance: '0', available: '0', blocked_models: [] },
    ],
    total: 3,
    page: 1,
    page_size: 100,
    total_pages: 1,
  })
})

// ---------- store: 参数拼装 / 筛选透传 / 详情 ----------

describe('bills useStore', () => {
  it('request 分页参数拼装(page→page/pageSize→page_size)+筛选透传+signal 透传', async () => {
    const controller = new AbortController()
    const result = await store.request(
      {
        page: 2,
        pageSize: 50,
        status: 'error',
        model: '  gpt-4o  ',
        api_key_id: 3,
        tenant_id: 42,
        dateRange: [1725091200000, 1725296400000],
      },
      { signal: controller.signal },
    )
    expect(listRequestLogs).toHaveBeenCalledWith(
      {
        page: 2,
        page_size: 50,
        status: 'error',
        model: 'gpt-4o',
        api_key_id: 3,
        tenant_id: 42,
        since_ms: 1725091200000,
        until_ms: 1725296400000,
      },
      controller.signal,
    )
    expect(result).toMatchObject({ total: 2 })
    expect(result.data).toHaveLength(2)
  })

  it('request 空筛选值转 undefined,不向接口透传', async () => {
    await store.request({
      page: 1,
      pageSize: 20,
      status: '',
      model: '',
      api_key_id: null,
      tenant_id: null,
      dateRange: null,
    })
    const [params, signal] = vi.mocked(listRequestLogs).mock.calls[0] as [
      ListRequestLogsParams,
      AbortSignal | undefined,
    ]
    expect(params).toMatchObject({ page: 1, page_size: 20 })
    expect(params.status).toBeUndefined()
    expect(params.model).toBeUndefined()
    expect(params.api_key_id).toBeUndefined()
    expect(params.tenant_id).toBeUndefined()
    expect(params.since_ms).toBeUndefined()
    expect(params.until_ms).toBeUndefined()
    expect(signal).toBeUndefined()
  })

  it('getDetail 透传 requestId 并返回详情', async () => {
    const detail = {
      ...makeLog({ request_id: 'req-9', status: 'error', error_detail: 'upstream 500: internal error' }),
      request_body: { messages: [] },
      response_body: '{}',
    } as RequestLogDetailData
    vi.mocked(getRequestLog).mockResolvedValue(detail)

    const result = await store.getDetail('req-9')
    expect(getRequestLog).toHaveBeenCalledWith('req-9')
    expect(result.request_id).toBe('req-9')
    expect(result.request_body).toEqual({ messages: [] })
    expect(result.error_detail).toBe('upstream 500: internal error')
  })

  it('formatDecimal 去尾零/空值/非法', () => {
    expect(formatDecimal('12.500000')).toBe('12.5')
    expect(formatDecimal('0.000000')).toBe('0')
    expect(formatDecimal(null)).toBe('—')
    expect(formatDecimal('abc')).toBe('abc')
  })

  it('formatDateTime 与状态映射/筛选选项', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime(new Date(2025, 2, 3, 4, 5).toISOString())).toBe('2025-03-03 04:05')
    expect(logStatusMap.interrupted).toEqual({ label: '中断', type: 'warning' })
    expect(statusFilterOptions.map((item) => item.value)).toEqual(['', 'success', 'error', 'interrupted'])
  })

  it('upstreamTypeConf 上游流向映射:local/external/null', () => {
    expect(upstreamTypeConf('local')).toEqual({ label: '本地', type: 'success' })
    expect(upstreamTypeConf('external')).toEqual({ label: '外部', type: 'info' })
    expect(upstreamTypeConf(null)).toEqual({ label: '未转发', type: 'default' })
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

describe('bills useController', () => {
  it('openDetail 经 useDialog 打开,props 带 requestId 且无底部按钮', () => {
    const { controller, wrapper } = mountController()
    controller.openDetail(makeLog({ request_id: 'req-abc' }))
    expect(useDialog).toHaveBeenCalledTimes(1)
    const call = vi.mocked(useDialog).mock.calls[0][0]
    expect(call.title).toBe('调用日志详情')
    expect(call.btn).toBe(false)
    expect(call.props).toEqual({ requestId: 'req-abc' })
    wrapper.unmount()
  })

  it('挂载后拉取租户列表填充下拉选项(排除平台租户 id=0)', async () => {
    const { controller, wrapper } = mountController()
    expect(listTenants).toHaveBeenCalledWith({ page: 1, page_size: 100 })
    await vi.waitFor(() => expect(controller.tenantOptions.value).toHaveLength(2))
    expect(controller.tenantOptions.value).toEqual([
      { label: '企业A', value: 3 },
      { label: '企业B', value: 7 },
    ])
    wrapper.unmount()
  })

  it('onTenantIdChange 更新 tenant_id 并以新筛选重新请求,清空回全量', async () => {
    const { controller, wrapper } = mountController()
    controller.onTenantIdChange(42)
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listRequestLogs).mock.calls.at(-1)?.[0] as ListRequestLogsParams
      expect(lastCall.tenant_id).toBe(42)
    })
    expect(controller.param.tenant_id).toBe(42)

    controller.onTenantIdChange(null)
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listRequestLogs).mock.calls.at(-1)?.[0] as ListRequestLogsParams
      expect(lastCall.tenant_id).toBeUndefined()
    })
    expect(controller.param.tenant_id).toBeNull()
    wrapper.unmount()
  })

  it('onDateRangeChange 更新 dateRange 并透传 since_ms/until_ms,清空回全时间', async () => {
    const { controller, wrapper } = mountController()
    const range: [number, number] = [1725091200000, 1725296400000]
    controller.onDateRangeChange(range)
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listRequestLogs).mock.calls.at(-1)?.[0] as ListRequestLogsParams
      expect(lastCall.since_ms).toBe(1725091200000)
      expect(lastCall.until_ms).toBe(1725296400000)
    })
    expect(controller.param.dateRange).toEqual(range)

    controller.onDateRangeChange(null)
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listRequestLogs).mock.calls.at(-1)?.[0] as ListRequestLogsParams
      expect(lastCall.since_ms).toBeUndefined()
      expect(lastCall.until_ms).toBeUndefined()
    })
    expect(controller.param.dateRange).toBeNull()
    wrapper.unmount()
  })
})

// ---------- controller: 上游列渲染 ----------

/** useTable 收到的列定义(列渲染断言入口) */
function upstreamColumn() {
  const options = useTableMock.mock.calls.at(-1)?.[0] as {
    columns: Array<DataTableColumn<RequestLogData>>
  }
  const column = options.columns.find((col) => col.key === 'upstream_type') as unknown as {
    render: (row: RequestLogData) => VNode
  }
  return column
}

/** 取 vnode 默认插槽渲染结果 */
function defaultSlot(vnode: VNode): unknown {
  return (vnode.children as { default: () => unknown }).default()
}

describe('bills useController 上游列', () => {
  it('无 provider_name:标签显示上游流向文案', () => {
    const { wrapper } = mountController()
    const vnode = upstreamColumn().render(makeLog({ upstream_type: 'external', provider_name: null }))
    expect(vnode.type).toBe(NTag)
    expect(defaultSlot(vnode)).toBe('外部')
    wrapper.unmount()
  })

  it('有 provider_name:标签直接显示供应商名,不做 JS 截断', () => {
    const { wrapper } = mountController()
    const name = 'OpenAI'
    const vnode = upstreamColumn().render(makeLog({ upstream_type: 'local', provider_name: name }))
    expect(vnode.type).toBe(NTag)
    expect(vnode.props).toMatchObject({ type: 'success' })

    const ellipsis = defaultSlot(vnode) as VNode
    expect(ellipsis.type).toBe(NEllipsis)
    expect(defaultSlot(ellipsis)).toBe(name)
    wrapper.unmount()
  })

  it('provider_name 超长:交给 NEllipsis 限宽省略(溢出才悬浮展示全名)', () => {
    const { wrapper } = mountController()
    const name = 'Azure-OpenAI-Enterprise-Gateway-Cluster'
    const vnode = upstreamColumn().render(makeLog({ provider_name: name }))

    const ellipsis = defaultSlot(vnode) as VNode
    // 限宽约束标签宽度,完整名字仍传给 NEllipsis(其内部按实际溢出决定省略号与 tooltip)
    expect(ellipsis.props).toMatchObject({ style: { maxWidth: '8rem' } })
    expect(defaultSlot(ellipsis)).toBe(name)
    wrapper.unmount()
  })
})
