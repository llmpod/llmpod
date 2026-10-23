import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  alarmChannelMap,
  alarmMetricMap,
  channelFormOptions,
  comparatorFormOptions,
  metricFormOptions,
  useStore,
  type AlarmRule,
} from '@/views/alarm/useStore'

const store = useStore()

beforeEach(() => vi.clearAllMocks())

describe('alarm useStore', () => {
  it('request 前端分页切片', async () => {
    const first = await store.request({ page: 1, pageSize: 2 })
    expect(first.data).toHaveLength(2)
    expect(first.total).toBe(3)

    const second = await store.request({ page: 2, pageSize: 2 })
    expect(second.data).toHaveLength(1)
  })

  it('save 无 id 新建:id 自增置顶且 enabled=true,notify 联动', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const before = (await store.request({ page: 1, pageSize: 100 })).total

    const result = await store.save({
      name: '新规则',
      metric: 'daily',
      comparator: '>',
      threshold: 100,
      channels: ['email'],
    })

    expect(result).toEqual({ mode: 'create', name: '新规则' })
    const after = await store.request({ page: 1, pageSize: 100 })
    expect(after.total).toBe(before + 1)
    expect(after.data[0]).toMatchObject({ name: '新规则', enabled: true })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('save 有 id 编辑:覆盖字段并返回 update 模式;id 不存在抛错不通知', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const existing = store.records.value[0]

    const result = await store.save({
      id: existing.id,
      name: '改名规则',
      metric: existing.metric,
      comparator: existing.comparator,
      threshold: 999,
      channels: existing.channels,
    })
    expect(result.mode).toBe('update')
    expect(store.records.value.find((r) => r.id === existing.id)?.threshold).toBe(999)

    await expect(
      store.save({ id: 9999, name: 'x', metric: 'daily', comparator: '<', threshold: 1, channels: [] }),
    ).rejects.toThrow('规则不存在')
    expect(listener).toHaveBeenCalledTimes(1) // 仅编辑成功那次
    unsubscribe()
  })

  it('remove 删除并 notify;id 不存在抛错不通知', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const target = store.records.value.at(-1)!

    await store.remove(target.id)
    expect(store.records.value.find((r) => r.id === target.id)).toBeUndefined()

    await expect(store.remove(9999)).rejects.toThrow('规则不存在')
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('指标/通知方式映射与表单选项齐全', () => {
    expect(Object.keys(alarmMetricMap).sort()).toEqual(['balance', 'daily', 'monthly'])
    expect(Object.keys(alarmChannelMap).sort()).toEqual(['email', 'feishu'])
    expect(metricFormOptions).toHaveLength(3)
    expect(comparatorFormOptions.map((item) => item.value)).toEqual(['<', '>'])
    expect(channelFormOptions).toHaveLength(2)
  })
})

type _Rule = AlarmRule
