import { beforeEach, describe, expect, it, vi } from 'vitest'
import { orderStatusMap, statusFilterOptions, useStore } from '@/views/orders/useStore'

const store = useStore()

beforeEach(() => vi.clearAllMocks())

describe('orders useStore', () => {
  it('request 状态过滤 + keyword 匹配单号或租户', async () => {
    const all = await store.request({ page: 1, pageSize: 20 })
    expect(all.total).toBe(3)

    const paid = await store.request({ page: 1, pageSize: 20, status: 'paid' })
    expect(paid.data.every((row) => row.status === 'paid')).toBe(true)

    const byKeyword = await store.request({ page: 1, pageSize: 20, keyword: 'ORD-2026-07-001' })
    expect(byKeyword.total).toBe(1)
  })

  it('refund 已支付转已退款并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const target = store.records.value.find((row) => row.status === 'paid')!

    await store.refund(target.id)
    expect(store.records.value.find((row) => row.id === target.id)?.status).toBe('refunded')
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('refund 边界:订单不存在/非已支付均抛错且不通知', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    await expect(store.refund(9999)).rejects.toThrow('订单不存在')

    const pending = store.records.value.find((row) => row.status === 'pending')!
    await expect(store.refund(pending.id)).rejects.toThrow('仅已支付订单可退款')

    expect(listener).not.toHaveBeenCalled()
    unsubscribe()
  })

  it('状态映射与筛选选项', () => {
    expect(orderStatusMap.paid).toEqual({ label: '已支付', type: 'success' })
    expect(orderStatusMap.pending.type).toBe('warning')
    expect(orderStatusMap.refunded.type).toBe('error')
    expect(statusFilterOptions[0]).toEqual({ label: '全部状态', value: '' })
  })
})
