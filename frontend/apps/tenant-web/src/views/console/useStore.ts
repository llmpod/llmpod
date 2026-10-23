import { ref } from 'vue'
import { formatDecimal } from '@repo/hooks'
import {
  getTenantDashboardScreen,
  type TenantDashboardScreenData,
  type TenantModelBriefData,
  type TenantRecentCallData,
} from '@/api/dashboard'

/** 顶部统计卡片 */
export interface StatCard {
  label: string
  value: string
}

/** 我的模型（本租户可用模型） */
export interface MyModel {
  id: number
  name: string
}

/** 最近调用 */
export interface RecentCall {
  /** HH:mm:ss（后端 ISO8601 截取,非法原样） */
  time: string
  input: number
  output: number
  /** 模型名（对外别名,空 → —） */
  model: string
  /** 延迟文案（xxms / —） */
  latency: string
  /** 结果色调:success=正常 error=错误 interrupted=中断 */
  tone: 'success' | 'error' | 'warning'
}

/** 账户信息卡（金额均为去尾零文案,元） */
export interface WalletCards {
  /** 总余额 */
  balance: string
  /** 在途冻结 */
  frozen: string
  /** 可用余额 */
  available: string
  /** 本月消费 */
  monthConsume: string
  /** 可用余额占总余额百分比 0-100（总余额 ≤0 时 0） */
  availablePercent: number
}

function trimZero(text: string): string {
  return text.replace(/\.?0+$/, '')
}

/** 大数紧凑格式：≥1亿 → x.x亿；≥1万 → x.x万；否则千分位 */
export function formatCompactNumber(value: number): string {
  if (value >= 100_000_000) return `${trimZero((value / 100_000_000).toFixed(2))}亿`
  if (value >= 10_000) return `${trimZero((value / 10_000).toFixed(2))}万`
  return value.toLocaleString()
}

/** Decimal 字符串去尾零展示（"234.500000" → "234.5","0.0000005" 不进科学计数法） */
export { formatDecimal }

/** ISO 时间截取 HH:mm:ss;非法原样 */
function formatClock(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 日期 ISO → M/D 轴标签 */
function formatAxisDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return `${date.getMonth() + 1}/${date.getDate()}`
}

const CALL_TONE_MAP: Record<string, RecentCall['tone']> = {
  success: 'success',
  error: 'error',
  interrupted: 'warning',
}

function toRecentCalls(rows: TenantRecentCallData[]): RecentCall[] {
  return rows.map((row) => ({
    time: formatClock(row.created_at),
    input: row.input_tokens,
    output: row.output_tokens,
    model: row.model ?? '—',
    latency: row.latency_ms === null ? '—' : `${row.latency_ms}ms`,
    tone: CALL_TONE_MAP[row.status] ?? 'success',
  }))
}

function toWallet(data: TenantDashboardScreenData): WalletCards {
  const balance = Number(data.balance)
  const available = Number(data.available)
  return {
    balance: formatDecimal(data.balance),
    frozen: formatDecimal(data.frozen_balance),
    available: formatDecimal(data.available),
    monthConsume: formatDecimal(data.month_consume),
    availablePercent: balance > 0 ? Math.round((available / balance) * 100) : 0,
  }
}

/** 顶部统计卡片 */
const statCards = ref<StatCard[]>([])

/** 近7日调用趋势 */
const trendDays = ref<string[]>([])
const trendCalls = ref<number[]>([])

/** 我的模型（本租户可用模型） */
const myModels = ref<MyModel[]>([])

/** 最近调用（top5） */
const recentCalls = ref<RecentCall[]>([])

/** 账户信息卡 */
const wallet = ref<WalletCards | null>(null)

/**
 * 工作台数据 store:数据来源于 /api/v1/tenant/dashboard/screen 整屏聚合
 * （窗口缺省 = 今日 00:00 → 当前;数据强制本租户,无越权面）。
 */
export function useStore() {
  /** 加载工作台数据,拉取后填充各视图模型 */
  const fetchConsole = async (signal?: AbortSignal) => {
    const data = await getTenantDashboardScreen(undefined, signal)
    statCards.value = [
      { label: '今日调用', value: formatCompactNumber(data.calls) },
      { label: '活跃模型', value: String(data.active_models) },
      { label: '成功率', value: `${trimZero(((1 - data.error_rate) * 100).toFixed(2))}%` },
      { label: '平均延迟', value: data.avg_ttft_ms === null ? '—' : `${data.avg_ttft_ms}ms` },
    ]
    trendDays.value = data.trend.map((point) => formatAxisDate(point.date))
    trendCalls.value = data.trend.map((point) => point.call_count)
    myModels.value = data.models.map((model: TenantModelBriefData) => ({
      id: model.id,
      name: model.name,
    }))
    recentCalls.value = toRecentCalls(data.recent_calls)
    wallet.value = toWallet(data)
  }

  return {
    statCards,
    trendDays,
    trendCalls,
    myModels,
    recentCalls,
    wallet,
    fetchConsole,
  }
}
