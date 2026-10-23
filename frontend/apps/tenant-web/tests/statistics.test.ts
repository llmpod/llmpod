// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/request-log', () => ({
  listTenantRequestLogs: vi.fn(),
  getTenantRequestLog: vi.fn(),
}))
vi.mock('@/api/api-key', () => ({ listTenantKeys: vi.fn() }))
vi.mock('@/views/statistics/RequestLogDetail.vue', () => ({ default: {} }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useDialog: vi.fn(),
    message: { error: vi.fn(), success: vi.fn() },
  }
})

import {
  listTenantRequestLogs,
  getTenantRequestLog,
  type RequestLogData,
  type RequestLogPageData,
  type RequestLogDetailData,
} from '@/api/request-log'
import { listTenantKeys, type ApiKeyData } from '@/api/api-key'
import { useDialog } from '@repo/hooks'
import {
  formatDecimal,
  formatNumber,
  formatDateTime,
  logStatusMap,
  upstreamTypeConf,
  useStore,
} from '@/views/statistics/useStore'
import { useController } from '@/views/statistics/useController'

function makeLog(overrides: Partial<RequestLogData> = {}): RequestLogData {
  return {
    id: 1,
    request_id: 'req-uuid-1',
    tenant_id: 7,
    api_key_id: 3,
    model_alias: 'deepseek-v3',
    served_model: 'ds-v3-internal',
    operation: 'chat.completions',
    protocol: 'openai',
    engine: 'vllm',
    upstream_type: 'external',
    retry_count: 0,
    prompt_tokens: 100,
    completion_tokens: 50,
    total_tokens: 150,
    cached_tokens: 0,
    cost: '0.001500',
    latency_ms: 320,
    status: 'success',
    http_status: 200,
    request_ref: null,
    response_ref: null,
    conv_prefix_hash: null,
    truncated: false,
    snapshot_disabled: false,
    error_message: null,
    created_at: '2026-08-26T02:30:00Z',
    ...overrides,
  }
}

function makeKey(overrides: Partial<ApiKeyData> = {}): ApiKeyData {
  return {
    id: 3,
    key_hash: 'a'.repeat(64),
    tenant_id: 7,
    name: '生产 key',
    allowed_models: [],
    rpm_limit: null,
    tpm_limit: null,
    total_token_quota: null,
    model_token_quotas: null,
    budget_limit: null,
    budget_spent: '0',
    budget_frozen: '0',
    status: 'active',
    expires_at: null,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    plaintext: 'sk-llmpod-statistics-key',
    ...overrides,
  }
}

function pageOf(items: RequestLogData[]): RequestLogPageData {
  return { items, total: items.length, page: 1, page_size: 20, total_pages: 1 }
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listTenantRequestLogs).mockResolvedValue(pageOf([makeLog()]))
  vi.mocked(listTenantKeys).mockResolvedValue([makeKey()])
})

// ---------- store ----------

describe('statistics useStore', () => {
  it('request 拼装分页与筛选参数(page_size 映射、空筛选转 undefined),signal 透传', async () => {
    const controller = new AbortController()
    await store.request(
      {
        page: 2,
        pageSize: 20,
        status: 'error',
        model: ' deepseek-v3 ',
        api_key_id: 3,
        dateRange: [1725091200000, 1725296400000],
      },
      { signal: controller.signal },
    )
    expect(listTenantRequestLogs).toHaveBeenCalledWith(
      {
        page: 2,
        page_size: 20,
        status: 'error',
        model: 'deepseek-v3',
        api_key_id: 3,
        since_ms: 1725091200000,
        until_ms: 1725296400000,
      },
      controller.signal,
    )
  })

  it('request 空筛选不下发参数,返回 items/total', async () => {
    const result = await store.request({
      page: 1,
      pageSize: 20,
      status: '',
      model: '  ',
      api_key_id: null,
      dateRange: null,
    })
    expect(listTenantRequestLogs).toHaveBeenCalledWith(
      {
        page: 1,
        page_size: 20,
        status: undefined,
        model: undefined,
        api_key_id: undefined,
        since_ms: undefined,
        until_ms: undefined,
      },
      undefined,
    )
    expect(result.total).toBe(1)
    expect(result.data[0].request_id).toBe('req-uuid-1')
  })

  it('getDetail 透传 request_id', async () => {
    const detail: RequestLogDetailData = {
      ...makeLog(),
      request_body: { model: 'deepseek-v3' },
      response_body: null,
    }
    vi.mocked(getTenantRequestLog).mockResolvedValue(detail)
    await expect(store.getDetail('req-uuid-1')).resolves.toMatchObject({
      request_id: 'req-uuid-1',
      request_body: { model: 'deepseek-v3' },
    })
    expect(getTenantRequestLog).toHaveBeenCalledWith('req-uuid-1')
  })

  it('request 请求失败向上抛错(useTable onError 接管)', async () => {
    vi.mocked(listTenantRequestLogs).mockRejectedValue(new Error('boom'))
    await expect(store.request({ page: 1, pageSize: 20 })).rejects.toThrow('boom')
  })

  it('格式化纯函数:Decimal/数字/时间/状态映射', () => {
    expect(formatDecimal(null)).toBe('—')
    expect(formatDecimal('0.001500')).toBe('0.0015')
    expect(formatNumber(null)).toBe('—')
    expect(formatNumber(12345)).toBe('12,345')
    expect(formatDateTime('bad')).toBe('bad')
    expect(formatDateTime(new Date(2026, 7, 26, 10, 30).toISOString())).toBe('2026-08-26 10:30')
    expect(logStatusMap.interrupted).toEqual({ label: '中断', type: 'warning' })
  })

  it('upstreamTypeConf 上游流向映射:local/external/null', () => {
    expect(upstreamTypeConf('local')).toEqual({ label: '本地', type: 'success' })
    expect(upstreamTypeConf('external')).toEqual({ label: '外部', type: 'info' })
    expect(upstreamTypeConf(null)).toEqual({ label: '未转发', type: 'default' })
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

describe('statistics useController', () => {
  it('挂载后拉取 Key 筛选选项', async () => {
    const { wrapper } = mountController()
    await vi.waitFor(() => expect(listTenantKeys).toHaveBeenCalled())
    wrapper.unmount()
  })

  it('Key 选项加载失败不阻塞页面', async () => {
    vi.mocked(listTenantKeys).mockRejectedValue(new Error('boom'))
    const { wrapper } = mountController()
    await vi.waitFor(() => expect(listTenantKeys).toHaveBeenCalled())
    wrapper.unmount()
  })

  it('openDetail 经 useDialog 打开详情弹窗(传 request_id)', async () => {
    const { controller, wrapper } = mountController()
    controller.openDetail(makeLog())
    expect(useDialog).toHaveBeenCalledTimes(1)
    expect(vi.mocked(useDialog).mock.calls[0][0].props).toEqual({ requestId: 'req-uuid-1' })
    wrapper.unmount()
  })

  it('状态/Key 筛选变更回第 1 页并请求', async () => {
    const { controller, wrapper } = mountController()
    vi.mocked(listTenantRequestLogs).mockClear()
    controller.onStatusChange('error')
    await vi.waitFor(() => expect(listTenantRequestLogs).toHaveBeenCalled())
    expect(vi.mocked(listTenantRequestLogs).mock.calls.at(-1)?.[0]).toMatchObject({
      status: 'error',
    })

    vi.mocked(listTenantRequestLogs).mockClear()
    controller.onApiKeyChange(3)
    await vi.waitFor(() => expect(listTenantRequestLogs).toHaveBeenCalled())
    expect(vi.mocked(listTenantRequestLogs).mock.calls.at(-1)?.[0]).toMatchObject({
      api_key_id: 3,
    })
    wrapper.unmount()
  })

  it('onDateRangeChange 更新 dateRange 并透传 since_ms/until_ms,清空回全时间', async () => {
    const { controller, wrapper } = mountController()
    const range: [number, number] = [1725091200000, 1725296400000]
    controller.onDateRangeChange(range)
    await vi.waitFor(() => {
      expect(vi.mocked(listTenantRequestLogs).mock.calls.at(-1)?.[0]).toMatchObject({
        since_ms: 1725091200000,
        until_ms: 1725296400000,
      })
    })
    expect(controller.param.dateRange).toEqual(range)

    controller.onDateRangeChange(null)
    await vi.waitFor(() => {
      const lastCall = vi.mocked(listTenantRequestLogs).mock.calls.at(-1)?.[0] as {
        since_ms?: number
        until_ms?: number
      }
      expect(lastCall.since_ms).toBeUndefined()
      expect(lastCall.until_ms).toBeUndefined()
    })
    expect(controller.param.dateRange).toBeNull()
    wrapper.unmount()
  })
})
