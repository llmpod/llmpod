import { request } from './request'

/** 流水类型：consume=消费 | topup=充值 | adjust=调额 | system=系统 */
export type TransactionType = 'consume' | 'topup' | 'adjust' | 'system'

/** 本租户余额三段（GET /tenant/wallet 响应；金额均为 Decimal 字符串，勿 parseFloat） */
export interface TenantWalletData {
  /** 总余额（元）：只被充值/调额/真实扣费改动，预扣不动 */
  balance: string
  /** 在途冻结（元）：预扣时 +、结算/解冻时 - */
  frozen_balance: string
  /** 可用余额（元）= balance - frozen_balance */
  available: string
}

/** 钱包流水行（对应后端 BalanceTransactionSchema；按时间倒序） */
export interface BalanceTransactionData {
  id: number
  /** 所属租户ID */
  tenant_id: number
  /** 触发 Key（充值/调额为空） */
  api_key_id: number | null
  /** Key 显示名快照（可空） */
  api_key_name: string | null
  /** 对外模型路由ID（可空） */
  route_id: number | null
  /** 对外模型名快照（可空） */
  route_name: string | null
  /** 关联 request_log 请求ID（可空） */
  request_id: string | null
  /** 流水类型：consume | topup | adjust | system */
  type: TransactionType
  /** 变动金额（元，Decimal 字符串，正入负出） */
  amount: string
  /** 该笔后余额（元，Decimal 字符串） */
  balance_after: string
  /** 附加信息。消费流水:operation/protocol/served_model/prompt_tokens/completion_tokens(上游未报 usage 时 token 键可能缺失或为 null);充值/调额:仅 reason */
  meta: Record<string, unknown>
  /** 流水时间（ISO8601） */
  created_at: string
}

/** 流水分页数据（对应后端 PageData[BalanceTransactionSchema]） */
export interface BalanceTransactionPageData {
  items: BalanceTransactionData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 流水分页查询参数 */
export interface ListTenantTransactionsParams {
  /** 页码，从 1 开始，默认 1 */
  page?: number
  /** 每页条数，1~100，默认 20 */
  page_size?: number
}

/** 查询本租户余额三段（GET /api/v1/tenant/wallet；无钱包行按 0 计。充值/扣减是平台运营动作，仅 admin 侧可调额） */
export function getTenantWallet(signal?: AbortSignal): Promise<TenantWalletData> {
  return request({ url: '/tenant/wallet', method: 'GET', signal })
}

/** 查询本租户流水分页（GET /api/v1/tenant/wallet/transactions，按时间倒序） */
export function listTenantTransactions(
  params: ListTenantTransactionsParams,
  signal?: AbortSignal,
): Promise<BalanceTransactionPageData> {
  return request({ url: '/tenant/wallet/transactions', method: 'GET', params, signal })
}
