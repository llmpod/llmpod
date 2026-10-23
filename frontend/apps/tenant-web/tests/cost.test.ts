// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 子组件 mock 占位:聚焦页面结构与 store 逻辑,不渲染子组件内部
vi.mock('@/views/cost/BalanceOverview.vue', () => ({ default: { render: () => null } }))
vi.mock('@/views/cost/TransactionList.vue', () => ({ default: { render: () => null } }))
vi.mock('@/views/cost/PlanPurchase.vue', () => ({ default: { render: () => null } }))
vi.mock('@/views/cost/BillManage.vue', () => ({ default: { render: () => null } }))
vi.mock('@/views/cost/InvoiceManage.vue', () => ({ default: { render: () => null } }))
vi.mock('@/api/wallet', () => ({
  getTenantWallet: vi.fn(),
  listTenantTransactions: vi.fn(),
}))

import { NTabs } from 'naive-ui'
import BalanceOverview from '@/views/cost/BalanceOverview.vue'
import TransactionList from '@/views/cost/TransactionList.vue'
import {
  getTenantWallet,
  listTenantTransactions,
  type BalanceTransactionData,
  type BalanceTransactionPageData,
  type TenantWalletData,
} from '@/api/wallet'
import { searchTabs } from '@/views/cost/tabs'
import CostIndex from '@/views/cost/index.vue'
import {
  transactionTypeMap,
  formatAmount,
  amountTone,
  formatDecimal,
  formatDateTime,
  formatTokens,
  useStore,
} from '@/views/cost/useStore'

function makeWallet(): TenantWalletData {
  return { balance: '234.500000', frozen_balance: '5.000000', available: '229.500000' }
}

function makeTx(overrides: Partial<BalanceTransactionData> = {}): BalanceTransactionData {
  return {
    id: 1,
    tenant_id: 7,
    api_key_id: 3,
    api_key_name: '生产 key',
    route_id: 1,
    route_name: 'deepseek-v3',
    request_id: 'req-uuid-1',
    type: 'consume',
    amount: '-0.012500',
    balance_after: '234.487500',
    meta: {
      operation: 'chat.completions',
      protocol: 'openai',
      served_model: 'deepseek-v3',
      prompt_tokens: 1000,
      completion_tokens: 500,
    },
    created_at: '2026-08-26T02:30:00Z',
    ...overrides,
  }
}

function pageOf(items: BalanceTransactionData[]): BalanceTransactionPageData {
  return { items, total: items.length, page: 1, page_size: 20, total_pages: 1 }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getTenantWallet).mockResolvedValue(makeWallet())
  vi.mocked(listTenantTransactions).mockResolvedValue(pageOf([makeTx()]))
})

// ---------- store ----------

describe('cost useStore', () => {
  it('fetchWallet 拉取余额三段,loading 回落,signal 透传', async () => {
    const store = useStore()
    const controller = new AbortController()
    const promise = store.fetchWallet(controller.signal)
    expect(store.walletLoading.value).toBe(true)
    await promise
    expect(getTenantWallet).toHaveBeenCalledWith(controller.signal)
    expect(store.wallet.value).toEqual(makeWallet())
    expect(store.walletLoading.value).toBe(false)
  })

  it('fetchWallet 失败时 loading 回落,错误向上抛', async () => {
    vi.mocked(getTenantWallet).mockRejectedValue(new Error('boom'))
    const store = useStore()
    await expect(store.fetchWallet()).rejects.toThrow('boom')
    expect(store.walletLoading.value).toBe(false)
    expect(store.wallet.value).toBeNull()
  })

  it('request 拼装流水分页参数(page_size 映射),返回 items/total', async () => {
    const store = useStore()
    const controller = new AbortController()
    const result = await store.request({ page: 2, pageSize: 20 }, { signal: controller.signal })
    expect(listTenantTransactions).toHaveBeenCalledWith(
      { page: 2, page_size: 20 },
      controller.signal,
    )
    expect(result.total).toBe(1)
    expect(result.data[0].type).toBe('consume')
  })

  it('格式化纯函数:类型映射/金额方向/去尾零/时间', () => {
    expect(transactionTypeMap.topup).toEqual({ label: '充值', type: 'success' })
    expect(transactionTypeMap.adjust.type).toBe('warning')
    expect(formatAmount('100.000000')).toBe('+100')
    expect(formatAmount('-0.012500')).toBe('-0.0125')
    expect(formatAmount('0.000000')).toBe('0')
    expect(amountTone('100')).toBe('success')
    expect(amountTone('-0.01')).toBe('error')
    expect(formatDecimal(null)).toBe('—')
    expect(formatDecimal('234.500000')).toBe('234.5')
    expect(formatDateTime(new Date(2026, 7, 26, 10, 30).toISOString())).toBe('2026-08-26 10:30')
  })

  it('formatTokens:prompt+completion 合计千分位,兼容旧版单值 tokens,无 usage 显示 —', () => {
    // 后端消费流水真实结构(见 proxyserver _billing_meta):无 tokens 键,只有 prompt/completion
    expect(formatTokens({ prompt_tokens: 1000, completion_tokens: 500 })).toBe('1,500')
    // 上游未报 completion(值为 0 时后端 or 链落 null):按已知部分合计
    expect(formatTokens({ prompt_tokens: 1000, completion_tokens: null })).toBe('1,000')
    expect(formatTokens({ prompt_tokens: null, completion_tokens: 500 })).toBe('500')
    // 充值/调额流水仅有 reason;消费但上游未报 usage
    expect(formatTokens({ reason: '季度充值' })).toBe('—')
    expect(formatTokens({ operation: 'chat', protocol: 'openai' })).toBe('—')
    expect(formatTokens({})).toBe('—')
    expect(formatTokens(null)).toBe('—')
    // 旧版后端写的单值 tokens
    expect(formatTokens({ tokens: 1500 })).toBe('1,500')
  })
})

// ---------- 页面结构 ----------

describe('cost tabs.ts 全局搜索声明', () => {
  it('页面已不分 tab,无子 tab 声明(账户流水与余额同页展示)', () => {
    expect(searchTabs).toEqual([])
  })
})

describe('cost index.vue 页面结构', () => {
  it('余额概览与账户流水同页上下展示,不再使用页签', () => {
    const wrapper = mount(CostIndex)
    expect(wrapper.findComponent(BalanceOverview).exists()).toBe(true)
    expect(wrapper.findComponent(TransactionList).exists()).toBe(true)
    expect(wrapper.findComponent(NTabs).exists()).toBe(false)
    wrapper.unmount()
  })
})
