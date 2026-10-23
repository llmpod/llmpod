import { beforeEach, describe, expect, it, vi } from 'vitest'
import { eventFormOptions, useStore } from '@/views/webhook/useStore'

const store = useStore()

beforeEach(() => vi.clearAllMocks())

describe('webhook useStore', () => {
  it('request 前端分页切片', async () => {
    const first = await store.request({ page: 1, pageSize: 2 })
    expect(first.data).toHaveLength(2)
    expect(first.total).toBe(3)
  })

  it('save 无 id 新建:enabled=true 且 lastTriggeredAt 占位,notify 联动', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    const result = await store.save({
      name: '新钩子',
      event: 'tenant.balance.low',
      url: 'https://hook.company.com/low',
    })

    expect(result).toEqual({ mode: 'create', name: '新钩子' })
    expect(store.records.value[0]).toMatchObject({
      name: '新钩子',
      enabled: true,
      lastTriggeredAt: '-',
    })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('save 有 id 编辑;id 不存在抛错不通知', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const existing = store.records.value[0]

    const result = await store.save({
      id: existing.id,
      name: '改名钩子',
      event: existing.event,
      url: 'https://hook.company.com/new',
    })
    expect(result.mode).toBe('update')
    expect(store.records.value[0].url).toBe('https://hook.company.com/new')

    await expect(
      store.save({ id: 9999, name: 'x', event: 'tenant.balance.low', url: 'u' }),
    ).rejects.toThrow('Webhook不存在')
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('test 发送测试事件:回写触发时间并 notify;id 不存在抛错', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const target = store.records.value[0]
    target.lastTriggeredAt = '-'

    await store.test(target.id)
    expect(store.records.value[0].lastTriggeredAt).not.toBe('-')
    expect(listener).toHaveBeenCalledTimes(1)

    await expect(store.test(9999)).rejects.toThrow('Webhook不存在')
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('事件类型选项三种', () => {
    expect(eventFormOptions.map((item) => item.value)).toEqual([
      'model.deploy.completed',
      'tenant.balance.low',
      'node.status.changed',
    ])
  })
})
