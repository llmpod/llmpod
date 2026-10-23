import { onBeforeUnmount } from 'vue'
import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, message } from '@repo/hooks'
import { useStore, orderStatusMap, statusFilterOptions, type Order } from './useStore'

export function useController() {
  const { records, request, refund, subscribe } = useStore()

  /** 顶部统计卡片:由本地记录派生,随退款联动 */
  const statCards = computed(() => {
    const list = records.value
    const income = list
      .filter((r) => r.status === 'paid')
      .reduce((sum, r) => sum + r.amount, 0)
    return [
      { label: '本月收入', value: `¥${income.toFixed(2)}` },
      { label: '今日订单', value: String(list.length) },
      {
        label: '待处理',
        value: String(list.filter((r) => r.status === 'pending').length),
        tone: 'warning' as const,
      },
      {
        label: '退款单',
        value: String(list.filter((r) => r.status === 'refunded').length),
        tone: 'error' as const,
      },
    ]
  })

  /** 退款:确认后状态置为已退款 */
  async function onRefund(row: Order) {
    await refund(row.id)
    message.success(`已退款:${row.orderNo}`)
  }

  /** 行操作:退款(仅已支付订单可见) */
  const selectActions: LmSelectAction<Order>[] = [
    {
      title: '退款',
      danger: true,
      show: (row) => row.status === 'paid',
      onClick: onRefund,
    },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, LmSearch, param, search, refresh, hasActiveQuery, reset } = useTable<
    Order,
    { keyword: string; status: string }
  >({
    request,
    param: { keyword: '', status: '' },
    columns: [
      { title: '订单号', key: 'orderNo', minWidth: 160, hideable: false },
      { title: '租户', key: 'tenant', width: 110 },
      { title: '套餐', key: 'packageName', width: 110 },
      {
        title: '金额',
        key: 'amount',
        width: 110,
        render: (row) => `¥${row.amount.toFixed(2)}`,
      },
      { title: '支付方式', key: 'payMethod', width: 110 },
      {
        title: '状态',
        key: 'status',
        width: 100,
        render: (row) => {
          const conf = orderStatusMap[row.status]
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
      { title: '下单日期', key: 'orderedAt', width: 120 },
      { title: '到期日', key: 'expiresAt', width: 120 },
      useOperate<Order>(selectActions, { width: 100 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
      // 窄屏卡片:订单号做主标题、金额提到卡首;异常单(待处理/已退款)才出左侧边轨
      card: {
        titleKey: 'orderNo',
        emphasisKey: 'amount',
        fieldKeys: ['tenant', 'status', 'orderedAt'],
        alertWhen: (row: Order) =>
          row.status === 'pending' ? 'warning' : row.status === 'refunded' ? 'error' : false,
      },
    },
  })

  // 订阅 store 变更:退款完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 状态筛选变更:回第 1 页并请求 */
  function onStatusChange(value: string | number) {
    param.status = String(value)
    void search()
  }

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    statCards,
    statusFilterOptions,
    param,
    onStatusChange,
    hasActiveQuery,
    reset,
  }
}
