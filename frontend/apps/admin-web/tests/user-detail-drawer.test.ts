// @vitest-environment happy-dom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { getDetailMock } = vi.hoisted(() => ({ getDetailMock: vi.fn() }))

vi.mock('@/api/access', () => ({ listRoles: vi.fn() }))
vi.mock('@/views/user/useStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/views/user/useStore')>()
  return { ...actual, useStore: () => ({ getDetail: getDetailMock }) }
})

import { listRoles } from '@/api/access'
import type { RoleData } from '@/api/access'
import type { UserData } from '@/api/user'
import UserDetailDrawer from '@/views/user/UserDetailDrawer.vue'

function makeUser(overrides: Partial<UserData> = {}): UserData {
  return {
    id: 1,
    tenant_id: 0,
    username: 'zhangsan',
    email: 'z@x.com',
    phone: null,
    status: 'active',
    role_ids: [1],
    last_login_at: null,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    tenant_balance: '0',
    tenant_frozen_balance: '0',
    tenant_available: '0',
    ...overrides,
  } as UserData
}

function makeRole(id: number, name: string): RoleData {
  return { id, tenant_id: 0, name, code: null, is_builtin: false, permission_ids: [] }
}

beforeEach(() => {
  vi.clearAllMocks()
  getDetailMock.mockResolvedValue(makeUser())
  vi.mocked(listRoles).mockResolvedValue([makeRole(1, '超级管理员'), makeRole(2, '运维')])
})

async function openDrawer(user: UserData): Promise<string> {
  const wrapper = mount(UserDetailDrawer, {
    props: { show: false, user },
    attachTo: document.body,
  })
  await wrapper.setProps({ show: true })
  await flushPromises()
  const text = document.body.textContent ?? ''
  wrapper.unmount()
  document.body.innerHTML = ''
  return text
}

describe('UserDetailDrawer 角色展示', () => {
  it('展示角色名称标签(而非数量),未知角色 ID 显示 #id', async () => {
    getDetailMock.mockResolvedValue(makeUser({ role_ids: [1, 2, 99] }))
    const text = await openDrawer(makeUser({ role_ids: [1, 2, 99] }))
    expect(text).toContain('超级管理员')
    expect(text).toContain('运维')
    expect(text).toContain('#99')
    expect(text).not.toContain('角色数')
  })

  it('无角色时显示 —', async () => {
    getDetailMock.mockResolvedValue(makeUser({ role_ids: [] }))
    const text = await openDrawer(makeUser({ role_ids: [] }))
    expect(text).toContain('角色')
    expect(text).not.toContain('超级管理员')
    expect(text).not.toContain('运维')
  })
})
