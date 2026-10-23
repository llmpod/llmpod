import { ref } from 'vue'

/** 订单状态:已支付 / 待支付 / 已退款 */
export type OrderStatus = 'paid' | 'pending' | 'refunded'

/** 列表行 / 本地数据记录 */
export interface Order {
  id: number
  orderNo: string
  tenant: string
  packageName: string
  amount: number
  payMethod: string
  status: OrderStatus
  orderedAt: string
  expiresAt: string
}

/** 状态筛选选项(含「全部」占位) */
export const statusFilterOptions = [
  { label: '全部状态', value: '' },
  { label: '已支付', value: 'paid' },
  { label: '待支付', value: 'pending' },
  { label: '已退款', value: 'refunded' },
]

/** 状态文案与标签类型映射 */
export const orderStatusMap: Record<
  OrderStatus,
  { label: string; type: 'success' | 'warning' | 'error' }
> = {
  paid: { label: '已支付', type: 'success' },
  pending: { label: '待支付', type: 'warning' },
  refunded: { label: '已退款', type: 'error' },
}

/** 模块级单例:表格与统计卡片共享同一份数据与变更通知 */
const records = ref<Order[]>([
  { id: 1, orderNo: 'ORD-2026-07-001', tenant: '企业A', packageName: '专业版', amount: 299, payMethod: '支付宝', status: 'paid', orderedAt: '2026-07-01', expiresAt: '2026-07-01' },
  { id: 2, orderNo: 'ORD-2026-07-002', tenant: '企业A', packageName: '企业版', amount: 999, payMethod: '银行转账', status: 'pending', orderedAt: '2026-07-01', expiresAt: '2026-07-01' },
  { id: 3, orderNo: 'ORD-2026-07-003', tenant: '企业A', packageName: '基础版', amount: 99, payMethod: '微信支付', status: 'refunded', orderedAt: '2026-07-01', expiresAt: '2026-07-01' },
])

/** 变更监听器:写操作完成后触发,供表格 refresh 联动 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/** 模拟远程延迟 */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 订单数据 store:表格请求、退款均走此单例,
 * 退款完成后 notify 触发表格与统计卡片联动刷新。
 */
export function useStore() {
  /** 分页 + 筛选请求(供 useTable.request) */
  const request = async (params: {
    page: number
    pageSize: number
    keyword?: string
    status?: string
  }) => {
    await delay(300)
    const keyword = (params.keyword ?? '').trim()
    let list = records.value
    if (params.status) list = list.filter((r) => r.status === params.status)
    if (keyword) {
      list = list.filter((r) => r.orderNo.includes(keyword) || r.tenant.includes(keyword))
    }
    const start = (params.page - 1) * params.pageSize
    return {
      data: list.slice(start, start + params.pageSize),
      total: list.length,
    }
  }

  /** 退款:已支付 → 已退款 */
  const refund = async (id: number): Promise<void> => {
    await delay(300)
    const row = records.value.find((r) => r.id === id)
    if (!row) throw new Error('订单不存在')
    if (row.status !== 'paid') throw new Error('仅已支付订单可退款')
    row.status = 'refunded'
    notify()
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { records, request, refund, subscribe }
}
