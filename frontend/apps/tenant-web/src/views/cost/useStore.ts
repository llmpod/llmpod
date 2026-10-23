import type { TableRequestContext } from '@repo/hooks'
import { ref } from 'vue'
import {
  getTenantWallet,
  listTenantTransactions,
  type TenantWalletData,
  type BalanceTransactionData,
  type TransactionType,
} from '@/api/wallet'

export type { TenantWalletData, BalanceTransactionData, TransactionType }

/** 流水类型文案与标签类型映射 */
export const transactionTypeMap: Record<
  TransactionType,
  { label: string; type: 'info' | 'success' | 'warning' | 'default' }
> = {
  consume: { label: '消费', type: 'info' },
  topup: { label: '充值', type: 'success' },
  adjust: { label: '调额', type: 'warning' },
  system: { label: '系统', type: 'default' },
}

/** Decimal 字符串去尾零展示("12.500000" → "12.5","0.0000005" 不进科学计数法);空值显示 — */
export { formatDecimal, formatSignedAmount as formatAmount } from '@repo/hooks'

/** 金额方向:正=入账(success),负=出账(error) */
export function amountTone(amount: string): 'success' | 'error' {
  return Number(amount) >= 0 ? 'success' : 'error'
}

/**
 * 消费流水 token 展示:meta.prompt_tokens + meta.completion_tokens 合计千分位;
 * 充值/调额无 usage(仅 reason)或上游未报 token 时显示 —;
 * 兼容旧版后端写的单值 meta.tokens。
 */
export function formatTokens(meta: Record<string, unknown> | null | undefined): string {
  if (!meta) return '—'
  const legacy = meta.tokens
  if (typeof legacy === 'number') return legacy.toLocaleString('zh-CN')
  const prompt = meta.prompt_tokens
  const completion = meta.completion_tokens
  if (typeof prompt !== 'number' && typeof completion !== 'number') return '—'
  const total =
    (typeof prompt === 'number' ? prompt : 0) + (typeof completion === 'number' ? completion : 0)
  return total.toLocaleString('zh-CN')
}

/** ISO8601 时间格式化为 YYYY-MM-DD HH:mm;空值显示 — */
export function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * 费用中心数据 store:余额三段 + 钱包流水分页,
 * 数据来源于 /api/v1/tenant/wallet(只读;充值/调额是平台运营动作,仅在 admin 侧)。
 */
export function useStore() {
  /** 余额三段(卡片展示) */
  const wallet = ref<TenantWalletData | null>(null)
  const walletLoading = ref(false)

  /** 拉取余额三段 */
  async function fetchWallet(signal?: AbortSignal): Promise<void> {
    walletLoading.value = true
    try {
      wallet.value = await getTenantWallet(signal)
    } finally {
      walletLoading.value = false
    }
  }

  /** 流水分页请求(供 useTable.request) */
  const request = async (
    params: { page: number; pageSize: number },
    ctx?: TableRequestContext,
  ) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    const result = await listTenantTransactions(
      { page: params.page, page_size: pageSize },
      ctx?.signal,
    )
    return { data: result.items, total: result.total }
  }

  return { wallet, walletLoading, fetchWallet, request }
}
