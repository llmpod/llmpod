// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/api-key', () => ({
  listTenantKeys: vi.fn(),
  createTenantKey: vi.fn(),
  updateTenantKey: vi.fn(),
  disableTenantKey: vi.fn(),
  enableTenantKey: vi.fn(),
  deleteTenantKey: vi.fn(),
  resetTenantKeyBudget: vi.fn(),
}))
vi.mock('@/api/model', () => ({ listTenantModels: vi.fn() }))
vi.mock('@/views/api-key/ApiKeyForm.vue', () => ({ default: {} }))
vi.mock('@/views/api-key/ApiKeyPlaintextDialog.vue', () => ({ default: {} }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useDialog: vi.fn(),
    useConfirm: vi.fn(),
    // useEntryAction 依赖路由注入,controller 测试统一 mock(其行为由 hooks 单测覆盖)
    useEntryAction: vi.fn(),
    message: { error: vi.fn(), success: vi.fn() },
  }
})

import {
  listTenantKeys,
  createTenantKey,
  updateTenantKey,
  disableTenantKey,
  enableTenantKey,
  deleteTenantKey,
  resetTenantKeyBudget,
  type ApiKeyData,
  type ApiKeyCreatedData,
} from '@/api/api-key'
import { listTenantModels } from '@/api/model'
import { useDialog, useConfirm, message } from '@repo/hooks'
import {
  formatDecimal,
  hasAdvancedLimits,
  isoToLocalInput,
  localInputToIso,
  useStore,
  type ApiKeyFormValues,
} from '@/views/api-key/useStore'
import { useController } from '@/views/api-key/useController'

function makeKey(overrides: Partial<ApiKeyData> = {}): ApiKeyData {
  return {
    id: 1,
    key_hash: 'a'.repeat(64),
    tenant_id: 7,
    name: '生产 key',
    allowed_models: ['deepseek-v3'],
    rpm_limit: 60,
    tpm_limit: null,
    total_token_quota: null,
    model_token_quotas: null,
    budget_limit: '100.000000',
    budget_spent: '12.500000',
    budget_frozen: '0.000000',
    status: 'active',
    expires_at: null,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    plaintext: 'sk-llmpod-abcdefghijklmnop',
    ...overrides,
  }
}

function makeForm(overrides: Partial<ApiKeyFormValues> = {}): ApiKeyFormValues {
  return {
    id: undefined,
    name: '新建Key',
    allowed_models: ['deepseek-v3'],
    rpm_limit: 50,
    tpm_limit: null,
    total_token_quota: null,
    budget_limit: '',
    expires_at: null,
    ...overrides,
  }
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listTenantKeys).mockResolvedValue([
    makeKey({ id: 1, name: 'key-a', status: 'active' }),
    makeKey({ id: 2, name: 'key-b', status: 'disabled' }),
    makeKey({ id: 3, name: 'key-c', status: 'active' }),
  ])
  vi.mocked(listTenantModels).mockResolvedValue([
    {
      id: 1,
      name: 'deepseek-v3',
      status: 'published',
      origin: 'manual',
      model_type: 'llm',
      description: null,
      context_length: null,
      tags: [],
      input_price_per_mtok: '2',
      output_price_per_mtok: '8',
      cached_input_price_per_mtok: null,
      failover_enabled: true,
      failover_max_retries: 2,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
  ])
})

// ---------- store ----------

describe('api-key useStore', () => {
  it('request 携带 name/status 查询参数(空串归 undefined)并透传 signal', async () => {
    const controller = new AbortController()
    await store.request({ keyword: '', status: '' }, { signal: controller.signal })
    expect(listTenantKeys).toHaveBeenCalledWith(
      { name: undefined, status: undefined },
      controller.signal,
    )

    await store.request({ keyword: 'prod', status: 'active' })
    expect(listTenantKeys).toHaveBeenLastCalledWith({ name: 'prod', status: 'active' }, undefined)
  })

  it('request 全量返回后按 page/pageSize 切片分页(不传按 page=1/pageSize=10 兜底)', async () => {
    const page1 = await store.request({ page: 1, pageSize: 2 })
    expect(page1.total).toBe(3)
    expect(page1.data.map((r) => r.name)).toEqual(['key-a', 'key-b'])

    const page2 = await store.request({ page: 2, pageSize: 2 })
    expect(page2.total).toBe(3)
    expect(page2.data.map((r) => r.name)).toEqual(['key-c'])

    const fallback = await store.request({})
    expect(fallback.total).toBe(3)
    expect(fallback.data).toHaveLength(3)
  })

  it('create 拼装签发参数(无 tenant_id,空预算转 null)并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const created: ApiKeyCreatedData = {
      api_key: makeKey({ id: 9, name: '新建Key' }),
      plaintext: 'sk-plaintext',
    }
    vi.mocked(createTenantKey).mockResolvedValue(created)

    const result = await store.create(makeForm())
    expect(createTenantKey).toHaveBeenCalledWith({
      name: '新建Key',
      allowed_models: ['deepseek-v3'],
      rpm_limit: 50,
      tpm_limit: null,
      total_token_quota: null,
      budget_limit: null,
      expires_at: null,
    })
    expect(result.plaintext).toBe('sk-plaintext')
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('create 空 allowed_models 恒传空数组(空=全部可用模型)', async () => {
    vi.mocked(createTenantKey).mockResolvedValue({
      api_key: makeKey(),
      plaintext: 'sk-x',
    })
    await store.create(makeForm({ allowed_models: [] }))
    expect(vi.mocked(createTenantKey).mock.calls[0][0].allowed_models).toEqual([])
  })

  it('update 显式传值并 notify;缺 id 抛错', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateTenantKey).mockResolvedValue(makeKey())

    const { name } = await store.update(makeForm({ id: 5, budget_limit: '88.5', allowed_models: [] }))
    expect(updateTenantKey).toHaveBeenCalledWith(5, expect.objectContaining({ budget_limit: '88.5', allowed_models: [] }))
    expect(name).toBe('新建Key')
    expect(listener).toHaveBeenCalledTimes(1)

    await expect(store.update(makeForm())).rejects.toThrow('缺少 Key ID')
    unsubscribe()
  })

  it('disable/enable/remove/resetBudget 透传并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(disableTenantKey).mockResolvedValue(makeKey({ status: 'disabled' }))
    vi.mocked(enableTenantKey).mockResolvedValue(makeKey({ status: 'active' }))
    vi.mocked(deleteTenantKey).mockResolvedValue(null)
    vi.mocked(resetTenantKeyBudget).mockResolvedValue(makeKey())

    await store.disable(3)
    expect(disableTenantKey).toHaveBeenCalledWith(3)
    await store.enable(3)
    expect(enableTenantKey).toHaveBeenCalledWith(3)
    await store.remove(3)
    expect(deleteTenantKey).toHaveBeenCalledWith(3)
    await store.resetBudget(3)
    expect(resetTenantKeyBudget).toHaveBeenCalledWith(3)
    expect(listener).toHaveBeenCalledTimes(4)
    unsubscribe()
  })

  it('formatDecimal:空值/去尾零/非数字三分支', () => {
    expect(formatDecimal(null)).toBe('—')
    expect(formatDecimal('12.500000')).toBe('12.5')
    expect(formatDecimal('0.000000')).toBe('0')
    expect(formatDecimal('abc')).toBe('abc')
  })

  it('hasAdvancedLimits:高级限流字段任一有值才默认展开', () => {
    // 新增(initial=undefined/null)与全空值 → 收起
    expect(hasAdvancedLimits(undefined)).toBe(false)
    expect(hasAdvancedLimits(null)).toBe(false)
    expect(
      hasAdvancedLimits({ rpm_limit: null, tpm_limit: null, total_token_quota: null, budget_limit: '' }),
    ).toBe(false)
    // 任一字段有值 → 展开(编辑回填时已设值可见)
    expect(hasAdvancedLimits(makeKey({ rpm_limit: 60, tpm_limit: null, total_token_quota: null, budget_limit: '' }))).toBe(true)
    expect(hasAdvancedLimits(makeKey({ rpm_limit: null, tpm_limit: 999, total_token_quota: null, budget_limit: '' }))).toBe(true)
    expect(hasAdvancedLimits(makeKey({ rpm_limit: null, tpm_limit: null, total_token_quota: 1_000_000, budget_limit: '' }))).toBe(true)
    expect(hasAdvancedLimits(makeKey({ rpm_limit: null, tpm_limit: null, total_token_quota: null, budget_limit: '100' }))).toBe(true)
  })

  it('localInputToIso/isoToLocalInput:空值/非法/合法互转', () => {
    expect(localInputToIso(null)).toBeNull()
    expect(localInputToIso('')).toBeNull()
    expect(localInputToIso('bad')).toBeNull()
    expect(localInputToIso('2026-08-26T10:00:00')).toBe(new Date(2026, 7, 26, 10, 0, 0).toISOString())

    expect(isoToLocalInput(null)).toBeNull()
    expect(isoToLocalInput('bad')).toBeNull()
    expect(isoToLocalInput(new Date(2026, 7, 26, 10, 0, 0).toISOString())).toBe(
      '2026-08-26T10:00:00',
    )
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

describe('api-key useController', () => {
  it('openCreate 经 useDialog 打开签发表单(带模型别名选项)', async () => {
    const { controller, wrapper } = mountController()
    await nextTick()
    await vi.waitFor(() => expect(listTenantModels).toHaveBeenCalled())
    controller.openCreate()
    expect(useDialog).toHaveBeenCalledTimes(1)
    expect(vi.mocked(useDialog).mock.calls[0][0].title).toBe('创建 API Key')
    wrapper.unmount()
  })

  it('禁用:确认后调用接口并提示;取消则不调用', async () => {
    const { wrapper } = mountController()
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(disableTenantKey).mockResolvedValue(makeKey({ status: 'disabled' }))

    // 经 store 层验证交互语义:useConfirm resolve → disable → message.success
    await expect(
      useConfirm({ title: '禁用 API Key' } as Parameters<typeof useConfirm>[0]),
    ).resolves.toBeUndefined()
    await store.disable(1)
    message.success('已禁用:key-a')
    expect(message.success).toHaveBeenCalledWith('已禁用:key-a')

    // 取消分支:reject 时不调用接口
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))
    await expect(useConfirm()).rejects.toThrow('cancel')
    expect(disableTenantKey).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('删除:确认后调用接口(返回 data:null)并提示', async () => {
    mountController()
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(deleteTenantKey).mockResolvedValue(null)
    await store.remove(1)
    expect(deleteTenantKey).toHaveBeenCalledWith(1)
    message.success('已删除:key-a')
    expect(message.success).toHaveBeenCalledWith('已删除:key-a')
  })

  it('重置预算:确认后调用接口并提示', async () => {
    mountController()
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(resetTenantKeyBudget).mockResolvedValue(makeKey())
    await store.resetBudget(1)
    expect(resetTenantKeyBudget).toHaveBeenCalledWith(1)
  })

  it('状态筛选变更回第 1 页并请求', async () => {
    const { controller, wrapper } = mountController()
    vi.mocked(listTenantKeys).mockClear()
    controller.onStatusChange('disabled')
    await vi.waitFor(() => expect(listTenantKeys).toHaveBeenCalled())
    wrapper.unmount()
  })
})
