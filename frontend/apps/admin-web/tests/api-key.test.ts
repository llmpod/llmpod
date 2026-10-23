// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/api-key', () => ({
  listApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  disableApiKey: vi.fn(),
  enableApiKey: vi.fn(),
  deleteApiKey: vi.fn(),
  updateApiKey: vi.fn(),
  resetApiKeyBudget: vi.fn(),
}))
vi.mock('@/api/tenant', () => ({ listTenants: vi.fn() }))
vi.mock('@/api/publish', () => ({ listPublishes: vi.fn() }))
vi.mock('@/views/api-key/ApiKeyForm.vue', () => ({ default: {} }))
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
  listApiKeys,
  createApiKey,
  disableApiKey,
  enableApiKey,
  deleteApiKey,
  updateApiKey,
  resetApiKeyBudget,
  type ApiKeyData,
  type ApiKeyCreatedData,
} from '@/api/api-key'
import { listTenants } from '@/api/tenant'
import { listPublishes } from '@/api/publish'
import { useDialog, useConfirm, message } from '@repo/hooks'
import type { LmSelectAction } from '@repo/naive-ui'
import { useStore, formatDecimal, hasAdvancedLimits, localInputToIso, isoToLocalInput, type ApiKeyFormValues } from '@/views/api-key/useStore'
import { useController } from '@/views/api-key/useController'

function makeKey(overrides: Partial<ApiKeyData> = {}): ApiKeyData {
  return {
    id: 1,
    key_hash: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2',
    tenant_id: 3,
    name: '生产环境 key',
    allowed_models: ['gpt-4o'],
    rpm_limit: 60,
    tpm_limit: 100000,
    total_token_quota: 1000000,
    model_token_quotas: { 'gpt-4o': 500000 },
    budget_limit: '100.00',
    budget_spent: '12.30',
    budget_frozen: '0.50',
    status: 'active',
    expires_at: null,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    plaintext: 'sk-llmpod-wxyzabcdef',
    ...overrides,
  } as ApiKeyData
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listApiKeys).mockResolvedValue([
    makeKey({ id: 1, name: 'key-a', status: 'active', tenant_id: 3 }),
    makeKey({ id: 2, name: 'key-b', status: 'disabled', tenant_id: null }),
  ])
  vi.mocked(listTenants).mockResolvedValue({
    items: [{ id: 3, name: '企业A', status: 'active', created_at: '', updated_at: '', user_count: 0, role_count: 0, balance: '0', frozen_balance: '0', available: '0', blocked_models: [] }],
    total: 1,
    page: 1,
    page_size: 100,
    total_pages: 1,
  })
  vi.mocked(listPublishes).mockResolvedValue([
    { route: { id: 1, name: 'gpt-4o', status: 'published', origin: 'manual', model_type: 'llm', description: null, context_length: null, tags: [], input_price_per_mtok: '2.5', output_price_per_mtok: '10', cached_input_price_per_mtok: null, failover_enabled: true, failover_max_retries: 2, created_at: '', updated_at: '' }, bindings: [] },
  ])
  useTableMock.mockReturnValue({
    LmTable: {},
    LmPage: {},
    LmRefresh: {},
    LmColumn: {},
    LmSearch: {},
    param: { keyword: '', status: '' },
    search: vi.fn(),
    refresh: vi.fn(),
    hasActiveQuery: { value: false },
    reset: vi.fn(),
  })
})

// ---------- store: 参数拼装 / 数据映射 / 错误分支 / 副作用 ----------

describe('api-key useStore', () => {
  it('request 调 listApiKeys 全量返回,status 前端过滤,signal 透传', async () => {
    const controller = new AbortController()
    const all = await store.request({}, { signal: controller.signal })
    expect(listApiKeys).toHaveBeenCalledWith(controller.signal)
    expect(all.total).toBe(2)

    const active = await store.request({ status: 'active' })
    expect(active.total).toBe(1)
    expect(active.data[0].name).toBe('key-a')

    const disabled = await store.request({ status: 'disabled' })
    expect(disabled.total).toBe(1)
    expect(disabled.data[0].name).toBe('key-b')
  })

  it('create: 参数拼装(空值转 undefined/null,expires_at 本地转 UTC) + notify 联动', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const created: ApiKeyCreatedData = {
      api_key: makeKey({ id: 99, name: '新Key' }),
      plaintext: 'sk-xxxxxx',
    }
    vi.mocked(createApiKey).mockResolvedValue(created)

    // 本地时间字符串 → new Date 按本时区解析 → toISOString 转真实 UTC
    const localInput = '2026-12-31T00:00:00'
    const expectedIso = new Date(localInput).toISOString()
    const model: ApiKeyFormValues = {
      tenant_id: 3,
      name: '新Key',
      allowed_models: ['gpt-4o'],
      rpm_limit: 60,
      tpm_limit: null,
      total_token_quota: null,
      budget_limit: '100',
      expires_at: localInput,
    }
    const result = await store.create(model)

    expect(createApiKey).toHaveBeenCalledWith({
      name: '新Key',
      tenant_id: 3,
      allowed_models: ['gpt-4o'],
      rpm_limit: 60,
      tpm_limit: null,
      total_token_quota: null,
      budget_limit: '100',
      expires_at: expectedIso,
    })
    expect(result).toBe(created)
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('update: 调 updateApiKey(allowed_models 恒传数组)并 notify;缺少 id 抛错', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateApiKey).mockResolvedValue(makeKey({ id: 1 }))

    await store.update({
      id: 1,
      tenant_id: null,
      name: '改名',
      allowed_models: [],
      rpm_limit: null,
      tpm_limit: null,
      total_token_quota: null,
      budget_limit: '',
      expires_at: null,
    })
    expect(updateApiKey).toHaveBeenCalledWith(1, expect.objectContaining({
      name: '改名',
      allowed_models: [], // 空数组=清空授权模型,而非 null
      budget_limit: null,
      expires_at: null,
    }))
    expect(listener).toHaveBeenCalledTimes(1)

    await expect(
      store.update({ id: undefined as unknown as number, tenant_id: null, name: 'x', allowed_models: [], rpm_limit: null, tpm_limit: null, total_token_quota: null, budget_limit: '', expires_at: null }),
    ).rejects.toThrow('缺少 Key ID')
    unsubscribe()
  })

  it('disable/enable/remove: 分别调对应接口并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(disableApiKey).mockResolvedValue(makeKey({ status: 'disabled' }))
    vi.mocked(enableApiKey).mockResolvedValue(makeKey({ status: 'active' }))
    vi.mocked(deleteApiKey).mockResolvedValue(null)

    await store.disable(1)
    expect(disableApiKey).toHaveBeenCalledWith(1)
    await store.enable(1)
    expect(enableApiKey).toHaveBeenCalledWith(1)
    await store.remove(1)
    expect(deleteApiKey).toHaveBeenCalledWith(1)
    expect(listener).toHaveBeenCalledTimes(3)
    unsubscribe()
  })

  it('resetBudget: 调 resetApiKeyBudget 并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(resetApiKeyBudget).mockResolvedValue(makeKey({ budget_spent: '0' }))

    await store.resetBudget(1)
    expect(resetApiKeyBudget).toHaveBeenCalledWith(1)
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('formatDecimal: 去尾零 / 空值 / 非法', () => {
    expect(formatDecimal('12.500000')).toBe('12.5')
    expect(formatDecimal('0.000000')).toBe('0')
    expect(formatDecimal(null)).toBe('—')
    expect(formatDecimal('abc')).toBe('abc')
  })

  it('hasAdvancedLimits: 高级限流字段任一有值才默认展开', () => {
    // 新增(initial=undefined/null)与全空值 → 收起
    expect(hasAdvancedLimits(undefined)).toBe(false)
    expect(hasAdvancedLimits(null)).toBe(false)
    expect(
      hasAdvancedLimits({ rpm_limit: null, tpm_limit: null, total_token_quota: null, budget_limit: '' }),
    ).toBe(false)
    // 任一字段有值 → 展开(编辑回填时已设值可见)
    expect(hasAdvancedLimits(makeKey({ rpm_limit: 60, tpm_limit: null, total_token_quota: null, budget_limit: '' }))).toBe(true)
    expect(hasAdvancedLimits(makeKey({ rpm_limit: null, tpm_limit: null, total_token_quota: null, budget_limit: '100' }))).toBe(true)
  })

  it('localInputToIso / isoToLocalInput: 本地↔UTC 互转与空值兜底', () => {
    // 本地时间字符串 → 真实 UTC 时刻(按运行时区计算)
    const localInput = '2026-12-31T00:00:00'
    const iso = localInputToIso(localInput)
    expect(iso).toBe(new Date(localInput).toISOString())
    // 往返:UTC → 本地 → 与原输入一致
    expect(isoToLocalInput(iso)).toBe(localInput)
    // 空值/非法(date-picker 空值契约是 null,空串会导致 naive-ui Invalid time value 崩溃)
    expect(localInputToIso(null)).toBeNull()
    expect(localInputToIso('')).toBeNull()
    expect(localInputToIso('bad')).toBeNull()
    expect(isoToLocalInput(null)).toBeNull()
    expect(isoToLocalInput('bad')).toBeNull()
  })
})

// ---------- controller: 编排 ----------

/** useTable 收到的 options(列定义断言入口) */
function tableOptions() {
  expect(useTableMock).toHaveBeenCalledTimes(1)
  return useTableMock.mock.calls[0][0] as {
    columns: Array<Record<string, unknown>>
  }
}

/** 操作列 render 产物中的行操作项(真实 onClick 编排入口) */
function operateActions(): LmSelectAction<ApiKeyData>[] {
  const operate = tableOptions().columns.find((col) => col.__lmOperate === true) as unknown as {
    render: (row: ApiKeyData) => { props: { actions: LmSelectAction<ApiKeyData>[] } }
  }
  expect(operate).toBeTruthy()
  return operate.render(makeKey()).props.actions
}

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

describe('api-key useController', () => {
  it('openCreate 经 useDialog 打开,props 带 tenants 与 modelAliases', async () => {
    const { controller, wrapper } = mountController()
    // 等待 onMounted fetchOptions 完成
    await vi.waitFor(() => expect(listTenants).toHaveBeenCalled())
    controller.openCreate()
    expect(useDialog).toHaveBeenCalledTimes(1)
    const call = vi.mocked(useDialog).mock.calls[0][0]
    expect(call.title).toBe('新建 API Key')
    expect(call.props).toHaveProperty('tenants')
    expect(call.props).toHaveProperty('modelAliases')
    wrapper.unmount()
  })

  it('onDisable: 操作列驱动,useConfirm 确认后调 disable + 提示(全角冒号)', async () => {
    const { wrapper } = mountController()
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(disableApiKey).mockResolvedValue(makeKey({ status: 'disabled' }))

    const disableAction = operateActions().find((action) => action.title === '禁用')!
    await disableAction.onClick!(makeKey({ id: 1, name: 'key-a', status: 'active' }))

    expect(useConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '禁用 API Key' }))
    expect(disableApiKey).toHaveBeenCalledWith(1)
    expect(message.success).toHaveBeenCalledWith('已禁用：key-a')
    wrapper.unmount()
  })

  it('onRemove: useConfirm 取消时不调接口', async () => {
    const { wrapper } = mountController()
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))

    const removeAction = operateActions().find((action) => action.title === '删除')!
    await removeAction.onClick!(makeKey({ id: 1, name: 'key-a' }))

    expect(useConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '删除 API Key' }))
    expect(deleteApiKey).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})