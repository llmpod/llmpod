import type { TableRequestContext } from '@repo/hooks'
import { ref } from 'vue'
import {
  listTenants,
  createTenant,
  getTenant,
  updateTenant,
  deleteTenant,
  getTenantsOverview,
  adjustTenantWallet,
  type TenantData,
  type TenantStatus,
  type AdjustTenantWalletPayload,
  type TenantWalletData,
  type WalletAdjustMode,
  type BlockedModelData,
} from '@/api/tenant'
import { listPublishes, type PublishData } from '@/api/publish'

export type { TenantData, TenantStatus, TenantWalletData, AdjustTenantWalletPayload, WalletAdjustMode }

/** 列表行 / 详情记录 */
export type Tenant = TenantData

/** 表单模型:id 可选(新增时无);新增时需同时填写租户超管账号 */
export interface TenantFormValues {
  id?: number
  name: string
  admin_username: string
  admin_password: string
  admin_email: string
  admin_phone: string
  /** 封禁的路由ID列表(勾选=封禁;全量替换语义:[]=清空封禁) */
  blocked_route_ids: number[]
}

/** 表单回填数据:编辑时取列表行/详情,blocked_models 映射为勾选初值 */
export interface TenantFormInitial {
  id?: number
  name?: string
  blocked_models?: BlockedModelData[]
}

/** 模型封禁表单候选:已发布路由全集(勾选=封禁,勾选初值由回填数据 blocked_models 推导) */
export interface BlockedRouteOption {
  route_id: number
  name: string
}

/** 状态筛选选项(含「全部」占位);status 为服务端过滤 */
export const tenantStatusFilterOptions = [
  { label: '全部', value: '' },
  { label: '启用', value: 'active' },
  { label: '禁用', value: 'disabled' },
]

/** 状态文案与标签类型映射(供表格/详情渲染) */
export const tenantStatusMap: Record<
  TenantStatus,
  { label: string; type: 'success' | 'error' }
> = {
  active: { label: '启用', type: 'success' },
  disabled: { label: '禁用', type: 'error' },
}

/** ISO8601 时间格式化为 YYYY-MM-DD HH:mm;空值显示 — */
export function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** Decimal 余额字符串去尾零展示（"100.000000" → "100"，"12.500000" → "12.5"） */
export { formatDecimal } from '@repo/hooks'

/**
 * Decimal 金额字符串十进制减法（minuend - subtrahend），BigInt 整型运算规避浮点误差
 * （如 856.65 - 432.55 浮点得 424.09999999999997）；结果保留操作数最大小数位。
 * 金额运算禁用浮点（docs/api-billing-frontend.md），展示侧再经 formatDecimal 去尾零。
 */
export function subtractDecimal(minuend: string, subtrahend: string): string {
  const scaleOf = (value: string): number => {
    const frac = value.trim().split('.')[1]
    return frac ? frac.length : 0
  }
  // 解析为按最小单位(10^-scale)计的整数,保留符号
  const toScaledInt = (value: string): bigint => {
    const text = value.trim() || '0'
    const negative = text.startsWith('-')
    const body = negative ? text.slice(1) : text
    const [intPart = '0', fracPart = ''] = body.split('.')
    const digits = BigInt(`${intPart || '0'}${fracPart}`)
    return negative ? -digits : digits
  }
  const scale = Math.max(scaleOf(minuend), scaleOf(subtrahend))
  const align = (value: string): bigint => toScaledInt(value) * 10n ** BigInt(scale - scaleOf(value))
  const result = align(minuend) - align(subtrahend)
  const negative = result < 0n
  const digits = (negative ? -result : result).toString().padStart(scale + 1, '0')
  const intPart = digits.slice(0, digits.length - scale)
  const fracPart = scale > 0 ? `.${digits.slice(-scale)}` : ''
  return `${negative ? '-' : ''}${intPart}${fracPart}`
}

/** 由发布路由推导表单封禁候选:仅已发布路由 */
export function buildBlockedRouteOptions(
  publishes: PublishData[],
): BlockedRouteOption[] {
  return publishes
    .filter((p) => p.route.status === 'published')
    .map((p) => ({
      route_id: p.route.id,
      name: p.route.name,
    }))
}

/** 变更监听器:写操作完成后触发,供表格/详情 refresh 联动 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/** 顶部概览统计卡(并入本页的原「租户概览」统计卡) */
export interface OverviewCard {
  label: string
  value: string
  tone?: 'success' | 'error'
}

/** 调额弹窗表单模型 */
export interface WalletAdjustFormValues {
  mode: WalletAdjustMode
  amount: number | null
  reason: string
}

const overviewCards = ref<OverviewCard[]>([])

/**
 * 租户数据 store:表格请求、表单保存、状态切换、删除、调额均走此单例,
 * 数据来源于 /api/v1/admin/tenants 接口;顶部统计卡来自 /admin/tenants/overview。
 */
export function useStore() {
  /** 分页 + 筛选请求(供 useTable.request);keyword 后端暂不支持,仅过滤当前页 */
  const request = async (params: {
    page: number
    pageSize: number
    keyword?: string
    status?: string
  }, ctx?: TableRequestContext) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    const result = await listTenants(
      {
        page: params.page,
        page_size: pageSize,
        status: (params.status || undefined) as TenantStatus | undefined,
      },
      ctx?.signal,
    )
    // 平台租户(id=0)不面向平台管理员展示,列表内直接过滤;
    // total 仍取服务端口径:平台租户占一个计数位,减 1 会让末页数据不可达
    let items = result.items.filter((r) => r.id !== 0)
    const keyword = (params.keyword ?? '').trim().toLowerCase()
    if (keyword) {
      items = items.filter((r) => r.name.toLowerCase().includes(keyword))
    }
    return { data: items, total: result.total }
  }

  /** 新增或更新:依据 model.id 判断;返回操作结果供调用方提示 */
  const save = async (
    model: TenantFormValues,
  ): Promise<{ mode: 'create' | 'update'; name: string }> => {
    let mode: 'create' | 'update'
    if (model.id) {
      await updateTenant(model.id, {
        name: model.name,
        // 全量替换语义:表单即最终状态,[]=清空封禁
        blocked_route_ids: model.blocked_route_ids,
      })
      mode = 'update'
    } else {
      // 创建租户同步创建租户超管(后端写死绑 tenant_admin 角色,建好即可登录)
      await createTenant({
        name: model.name,
        admin_username: model.admin_username,
        admin_password: model.admin_password,
        admin_email: model.admin_email || undefined,
        admin_phone: model.admin_phone || undefined,
        blocked_route_ids: model.blocked_route_ids,
      })
      mode = 'create'
    }
    notify()
    return { mode, name: model.name }
  }

  /** 启用/禁用:active ↔ disabled 切换,返回切换后状态 */
  const toggleStatus = async (row: Tenant): Promise<TenantStatus> => {
    const next: TenantStatus = row.status === 'active' ? 'disabled' : 'active'
    await updateTenant(row.id, { status: next })
    notify()
    return next
  }

  /** 删除租户(后端级联清空该租户用户及其角色后直接删除) */
  const remove = async (id: number): Promise<void> => {
    await deleteTenant(id)
    notify()
  }

  /** 租户详情 */
  const getDetail = async (id: number): Promise<Tenant> => {
    return getTenant(id)
  }

  /** 封禁模型表单候选:已发布路由全集(勾选初值由调用方从 blocked_models 推导) */
  const loadBlockedRouteOptions = async (
    signal?: AbortSignal,
  ): Promise<BlockedRouteOption[]> => {
    const publishes = await listPublishes(signal)
    return buildBlockedRouteOptions(publishes)
  }

  /** 拉取顶部概览统计卡(总租户/正常/冻结·预逾期/总用户;口径见 /admin/tenants/overview) */
  const fetchOverview = async (signal?: AbortSignal): Promise<void> => {
    const data = await getTenantsOverview(undefined, signal)
    overviewCards.value = [
      { label: '总租户数', value: String(data.total_tenants) },
      { label: '正常', value: String(data.active_tenants), tone: 'success' },
      { label: '冻结/预逾期', value: String(data.disabled_tenants), tone: 'error' },
      { label: '总用户数', value: String(data.total_users) },
    ]
  }

  /** 人工调额(add/subtract/override);返回调整后钱包供弹窗提示,notify 联动表格/详情/统计卡刷新 */
  const adjustWallet = async (
    tenantId: number,
    payload: AdjustTenantWalletPayload,
  ): Promise<TenantWalletData> => {
    const wallet = await adjustTenantWallet(tenantId, payload)
    notify()
    return wallet
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return {
    overviewCards,
    request,
    save,
    toggleStatus,
    remove,
    getDetail,
    loadBlockedRouteOptions,
    fetchOverview,
    adjustWallet,
    subscribe,
  }
}
