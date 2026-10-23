import { beforeEach, describe, expect, it, vi } from 'vitest'
import { packageTypeMap, typeFormOptions, useStore, type PackageFormValues } from '@/views/package/useStore'

const store = useStore()

beforeEach(() => vi.clearAllMocks())

function makeForm(overrides: Partial<PackageFormValues> = {}): PackageFormValues {
  return {
    name: '新套餐',
    type: 'monthly',
    price: 199,
    tokenQuota: '2000万',
    qps: null,
    concurrency: null,
    ...overrides,
  }
}

describe('package useStore', () => {
  it('request 前端分页切片', async () => {
    const first = await store.request({ page: 1, pageSize: 5 })
    expect(first.data).toHaveLength(5)
    expect(first.total).toBe(8)
  })

  it('save 无 id 新建:置顶 enabled=true subscribers=0,空数值回退默认(qps 50/concurrency 10)', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const before = store.records.value.length

    const result = await store.save(makeForm())

    expect(result).toEqual({ mode: 'create', name: '新套餐' })
    expect(store.records.value.length).toBe(before + 1)
    expect(store.records.value[0]).toMatchObject({
      name: '新套餐',
      enabled: true,
      subscribers: 0,
      qps: 50,
      concurrency: 10,
      price: 199,
    })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('save 有 id 更新:仅覆盖表单字段,null 数值回退 0', async () => {
    const target = store.records.value[0]
    const originalEnabled = target.enabled
    const originalSubscribers = target.subscribers

    const result = await store.save(
      makeForm({ id: target.id, name: '改名套餐', price: null, qps: null, concurrency: null }),
    )

    expect(result).toEqual({ mode: 'update', name: '改名套餐' })
    const updated = store.records.value.find((row) => row.id === target.id)!
    expect(updated).toMatchObject({ name: '改名套餐', price: 0, qps: 0, concurrency: 0 })
    // 表单外的字段不动
    expect(updated.enabled).toBe(originalEnabled)
    expect(updated.subscribers).toBe(originalSubscribers)
  })

  it('remove 删除并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const target = store.records.value[0]

    await store.remove(target.id)
    expect(store.records.value.find((row) => row.id === target.id)).toBeUndefined()
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('类型映射与选项', () => {
    expect(packageTypeMap).toEqual({ monthly: '包月', yearly: '包年' })
    expect(typeFormOptions).toHaveLength(2)
  })
})
