// @vitest-environment happy-dom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/access', () => ({ listRoles: vi.fn() }))
vi.mock('@/views/user/useStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/views/user/useStore')>()
  return { ...actual, useStore: () => ({ save: vi.fn() }) }
})

import { listRoles } from '@/api/access'
import type { RoleData } from '@/api/access'
import type { UserData } from '@/api/user'
import UserForm from '@/views/user/UserForm.vue'

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
  vi.mocked(listRoles).mockResolvedValue([makeRole(1, '超级管理员'), makeRole(2, '运维')])
})

describe('UserForm 角色回填', () => {
  it('编辑态回填已有角色(选项加载后显示对应角色标签)', async () => {
    const wrapper = mount(UserForm, {
      props: { initial: makeUser({ role_ids: [1, 2] }) },
    })
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('超级管理员')
    expect(text).toContain('运维')
  })

  it('新建态角色为空(不显示任何角色标签)', async () => {
    const wrapper = mount(UserForm, { props: {} })
    await flushPromises()
    expect(wrapper.text()).not.toContain('超级管理员')
    expect(wrapper.text()).not.toContain('运维')
  })
})
