import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/user', () => ({
  listUsers: vi.fn(),
  createUser: vi.fn(),
  getUser: vi.fn(),
  updateUser: vi.fn(),
  deleteUser: vi.fn(),
}))

import { createUser, deleteUser, getUser, listUsers, updateUser, type UserData } from '@/api/user'
import {
  formatDateTime,
  statusFilterOptions,
  userStatusFormOptions,
  userStatusMap,
  useStore,
  type UserFormValues,
} from '@/views/user/useStore'

function makeUser(overrides: Partial<UserData> = {}): UserData {
  return {
    id: 1,
    username: 'zhangsan',
    email: 'z@x.com',
    phone: null,
    status: 'active',
    role_ids: [1],
    tenant_balance: '100.000000',
    tenant_frozen_balance: '5.000000',
    tenant_available: '95.000000',
    created_at: '2025-01-01T00:00:00Z',
    ...overrides,
  } as UserData
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listUsers).mockResolvedValue({
    items: [
      makeUser({ id: 1, username: 'zhangsan', status: 'active' }),
      makeUser({ id: 2, username: 'lisi', email: null, status: 'disabled' }),
    ],
    total: 2,
    page: 1,
    page_size: 20,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listUsers>>)
})

describe('user useStore', () => {
  it('request 后端分页 + keyword/status 仅过滤当前页', async () => {
    const result = await store.request({ page: 1, pageSize: 20 })
    expect(listUsers).toHaveBeenCalledWith({ page: 1, page_size: 20 }, undefined)
    expect(result.total).toBe(2)
    expect(store.pageItems.value).toHaveLength(2)
    expect(store.total.value).toBe(2)

    const filtered = await store.request({ page: 1, pageSize: 20, keyword: 'LISI' })
    expect(filtered.data).toHaveLength(1)
    expect(filtered.data[0].username).toBe('lisi')

    const byStatus = await store.request({ page: 1, pageSize: 20, status: 'disabled' })
    expect(byStatus.data[0].username).toBe('lisi')
  })

  it('save 无 id 创建:tenant_id=0 平台用户,空邮箱/手机传 undefined', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(createUser).mockResolvedValue(makeUser())

    const result = await store.save({
      username: 'newuser',
      password: 'secret',
      role_ids: [1],
      email: '',
      phone: '',
      status: 'active',
    } as UserFormValues)

    expect(result).toEqual({ mode: 'create', name: 'newuser' })
    expect(createUser).toHaveBeenCalledWith({
      username: 'newuser',
      password: 'secret',
      role_ids: [1],
      email: undefined,
      phone: undefined,
    })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('save 有 id 更新:密码留空不传 password', async () => {
    vi.mocked(updateUser).mockResolvedValue(makeUser())
    const result = await store.save({
      id: 1,
      username: 'zhangsan',
      password: '',
      role_ids: [1],
      email: 'new@x.com',
      phone: '',
      status: 'disabled',
    } as UserFormValues)

    expect(result.mode).toBe('update')
    expect(updateUser).toHaveBeenCalledWith(1, {
      password: undefined,
      email: 'new@x.com',
      phone: undefined,
      status: 'disabled',
    })
    expect(createUser).not.toHaveBeenCalled()
  })

  it('toggleStatus:active↔disabled 互切并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateUser).mockResolvedValue(makeUser())

    const next = await store.toggleStatus(makeUser({ id: 1, status: 'active' }))
    expect(next).toBe('disabled')
    expect(updateUser).toHaveBeenCalledWith(1, { status: 'disabled' })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('remove/getDetail 透传,remove 后 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(deleteUser).mockResolvedValue(undefined)
    await store.remove(1)
    expect(deleteUser).toHaveBeenCalledWith(1)

    vi.mocked(getUser).mockResolvedValue(makeUser({ id: 1 }))
    expect(await store.getDetail(1)).toMatchObject({ id: 1 })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('formatDateTime 与状态选项常量', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime(new Date(2025, 2, 3, 4, 5).toISOString())).toBe('2025-03-03 04:05')
    expect(userStatusMap.disabled).toEqual({ label: '禁用', type: 'error' })
    expect(statusFilterOptions[0]).toEqual({ label: '全部状态', value: '' })
    expect(userStatusFormOptions).toHaveLength(2) // 表单无「全部」
  })
})
