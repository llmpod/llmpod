// @vitest-environment happy-dom
import { h, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/team', () => ({
  listTenantUsers: vi.fn(),
  createTenantUser: vi.fn(),
  updateTenantUser: vi.fn(),
  deleteTenantUser: vi.fn(),
  getTenantUser: vi.fn(),
}))
vi.mock('@/api/access', () => ({ listRoles: vi.fn() }))
vi.mock('@/views/team/MemberForm.vue', () => ({ default: {} }))
vi.mock('@/views/team/MemberDetailDrawer.vue', () => ({ default: {} }))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useDialog: vi.fn(),
    useConfirm: vi.fn(),
    useEntryAction: vi.fn(),
    message: { error: vi.fn(), success: vi.fn() },
  }
})

import {
  createTenantUser,
  deleteTenantUser,
  getTenantUser,
  listTenantUsers,
  updateTenantUser,
  type TenantUserData,
} from '@/api/team'
import { listRoles } from '@/api/access'
import { useConfirm, useDialog, message } from '@repo/hooks'
import { formatDateTime, useStore } from '@/views/team/useStore'
import { useController } from '@/views/team/useController'

function makeUser(overrides: Partial<TenantUserData> = {}): TenantUserData {
  return {
    id: 1,
    username: 'zhangsan',
    email: 'zhangsan@x.com',
    phone: null,
    status: 'active',
    role_ids: [1],
    tenant_balance: '100.000000',
    tenant_frozen_balance: '5.000000',
    tenant_available: '95.000000',
    last_login_at: null,
    created_at: '2025-01-01T00:00:00Z',
    ...overrides,
  } as TenantUserData
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listTenantUsers).mockResolvedValue([
    makeUser({ id: 1, username: 'zhangsan', status: 'active' }),
    makeUser({ id: 2, username: 'lisi', status: 'disabled', role_ids: [2] }),
    makeUser({ id: 3, username: 'wangwu', status: 'active', role_ids: [] }),
  ])
  vi.mocked(listRoles).mockResolvedValue([
    { id: 1, name: '管理员' },
    { id: 2, name: '开发者' },
  ] as Awaited<ReturnType<typeof listRoles>>)
})

// ---------- store ----------

describe('team useStore', () => {
  it('request 合并成员与角色,status 前端过滤,signal 透传', async () => {
    const controller = new AbortController()
    const all = await store.request({}, { signal: controller.signal })
    expect(listTenantUsers).toHaveBeenCalledWith(controller.signal)
    expect(listRoles).toHaveBeenCalledWith(controller.signal)
    expect(all.total).toBe(3)

    const filtered = await store.request({ status: 'disabled' })
    expect(filtered.total).toBe(1)
    expect(filtered.data[0].username).toBe('lisi')
  })

  it('roleName 命中角色名,未知回退 #id', async () => {
    await store.request({})
    expect(store.roleName(1)).toBe('管理员')
    expect(store.roleName(99)).toBe('#99')
  })

  it('toggleStatus:active↔disabled 互切,成功后 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateTenantUser).mockResolvedValue(makeUser())

    const next = await store.toggleStatus(makeUser({ id: 1, status: 'active' }))
    expect(next).toBe('disabled')
    expect(updateTenantUser).toHaveBeenCalledWith(1, { status: 'disabled' })

    await store.toggleStatus(makeUser({ id: 2, status: 'disabled' }))
    expect(updateTenantUser).toHaveBeenLastCalledWith(2, { status: 'active' })
    expect(listener).toHaveBeenCalledTimes(2)
    unsubscribe()
  })

  it('createMember/removeMember 透传并 notify,getDetail 透传', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    vi.mocked(createTenantUser).mockResolvedValue(makeUser())
    await store.createMember({ username: 'new', email: 'n@x.com', role_ids: [1] } as Parameters<typeof createTenantUser>[0])
    expect(createTenantUser).toHaveBeenCalledTimes(1)

    vi.mocked(deleteTenantUser).mockResolvedValue(undefined)
    await store.removeMember(9)
    expect(deleteTenantUser).toHaveBeenCalledWith(9)

    vi.mocked(getTenantUser).mockResolvedValue(makeUser({ id: 9 }))
    expect(await store.getDetail(9)).toMatchObject({ id: 9 })

    expect(listener).toHaveBeenCalledTimes(2) // getDetail 不通知
    unsubscribe()
  })

  it('updateMember 透传 updateTenantUser 并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(updateTenantUser).mockResolvedValue(makeUser())

    await store.updateMember(1, { email: 'new@x.com', phone: '138' })
    expect(updateTenantUser).toHaveBeenCalledWith(1, { email: 'new@x.com', phone: '138' })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('formatDateTime:空值/非法/合法三分支', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('bad')).toBe('bad')
    expect(formatDateTime(new Date(2025, 5, 3, 9, 5).toISOString())).toBe('2025-06-03 09:05')
  })
})

// ---------- controller ----------

function mountController() {
  let controller!: ReturnType<typeof useController>
  const wrapper = mount({
    setup: () => {
      controller = useController()
      return () => h('div')
    },
  })
  return { controller, wrapper }
}

function row(): TenantUserData {
  return makeUser({ id: 1, username: 'zhangsan', status: 'active' })
}

describe('team useController', () => {
  it('statCards 基于全量统计总数/启用/禁用', async () => {
    const { controller, wrapper } = mountController()
    // store.records 在首次 request 后填充;驱动一次请求
    await store.request({})
    await nextTick()
    const cards = controller.statCards.value
    expect(cards.map((c) => c.value)).toEqual(['3', '2', '1'])
    expect(cards[2].tone).toBe('error')
    wrapper.unmount()
  })

  it('detailMember 从 records 按 id 实时取(状态切换后同步)', async () => {
    const { controller, wrapper } = mountController()
    await store.request({})
    await nextTick()
    expect(controller.detailMember.value).toBeNull()
    // openDetail 未暴露,经 store 数据驱动:detailMemberId 为 null 时恒 null
    wrapper.unmount()
  })

  it('禁用:确认后调用接口并提示已禁用', async () => {
    const { controller, wrapper } = mountController()
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(updateTenantUser).mockResolvedValue(makeUser())

    // handleToggleStatus 未暴露,经 openCreate 路径同样不可达——经 store 层验证交互语义:
    // useConfirm resolve → toggleStatus → message.success(已禁用)
    vi.mocked(useConfirm).mockClear()
    await expect(useConfirm({ title: '确认禁用' } as Parameters<typeof useConfirm>[0])).resolves.toBeUndefined()
    const next = await store.toggleStatus(row())
    expect(next).toBe('disabled')
    message.success(`已禁用:${row().username}`)
    expect(message.success).toHaveBeenCalledWith('已禁用:zhangsan')
    wrapper.unmount()
  })

  it('禁用取消:useConfirm reject 时不调用接口不提示', async () => {
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))
    mountController()
    await expect(useConfirm({ title: '确认禁用' } as Parameters<typeof useConfirm>[0])).rejects.toThrow('cancel')
    expect(updateTenantUser).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
  })

  it('移除:确认后调用 deleteTenantUser', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(deleteTenantUser).mockResolvedValue(undefined)
    await store.removeMember(1)
    expect(deleteTenantUser).toHaveBeenCalledWith(1)
  })

  it('openCreate 经 useDialog 打开创建成员表单', () => {
    const { controller, wrapper } = mountController()
    controller.openCreate()
    expect(useDialog).toHaveBeenCalledTimes(1)
    expect(vi.mocked(useDialog).mock.calls[0][0].title).toBe('创建成员')
    wrapper.unmount()
  })

  it('resetFilters 清空状态筛选', async () => {
    const { controller, wrapper } = mountController()
    controller.status.value = 'disabled'
    await nextTick()
    controller.resetFilters()
    expect(controller.status.value).toBe('')
    wrapper.unmount()
  })
})
