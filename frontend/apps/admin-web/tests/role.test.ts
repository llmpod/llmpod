import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/access', () => ({
  listPermissions: vi.fn(),
  listRoles: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
  listRolePermissions: vi.fn(),
  replaceRolePermissions: vi.fn(),
}))

import {
  createRole,
  deleteRole,
  listPermissions,
  listRolePermissions,
  listRoles,
  replaceRolePermissions,
  updateRole,
  type PermissionData,
  type RoleData,
} from '@/api/access'
import { permissionTreeData, roleTypeMap, useStore } from '@/views/role/useStore'

function makePermission(overrides: Partial<PermissionData> = {}): PermissionData {
  return {
    id: 1,
    scope: 'platform',
    resource: 'user',
    action: 'read',
    description: '查看用户',
    ...overrides,
  } as PermissionData
}

function makeRole(overrides: Partial<RoleData> = {}): RoleData {
  return { id: 1, name: '管理员', is_builtin: false, ...overrides } as RoleData
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listRoles).mockResolvedValue([
    makeRole({ id: 1, name: '管理员', is_builtin: true }),
    makeRole({ id: 2, name: '开发', is_builtin: false }),
  ])
})

describe('role useStore', () => {
  it('request 前端分页切片', async () => {
    const first = await store.request({ page: 1, pageSize: 1 })
    expect(first.data[0].name).toBe('管理员')
    expect(first.total).toBe(2)

    const second = await store.request({ page: 2, pageSize: 1 })
    expect(second.data[0].name).toBe('开发')
  })

  it('loadPermissions 仅请求 platform 域,幂等缓存', async () => {
    vi.mocked(listPermissions).mockImplementation(async () => [
      makePermission({ id: 1, scope: 'platform', resource: 'user', action: 'read' }),
    ])
    await store.loadPermissions()
    await store.loadPermissions()
    expect(listPermissions).toHaveBeenCalledTimes(1)
    expect(listPermissions).toHaveBeenCalledWith('platform')
    expect(listPermissions).not.toHaveBeenCalledWith('tenant')
  })

  it('permissionTreeData 按 scope:resource 聚合,标题带 scope 文案', async () => {
    vi.mocked(listPermissions).mockImplementation(async () => [
      makePermission({ id: 1, scope: 'platform', resource: 'user', action: 'read' }),
      makePermission({ id: 2, scope: 'platform', resource: 'user', action: 'write', description: '' }),
      makePermission({ id: 3, scope: 'platform', resource: 'billing', description: '查看账单' }),
    ])
    await store.loadPermissions(true)
    const tree = permissionTreeData.value
    const platformUser = tree.find((group) => group.key === 'platform:user')
    expect(platformUser).toBeTruthy()
    expect(platformUser!.label).toContain('平台')
    expect(platformUser!.children).toHaveLength(2)
    // description 为空回退 resource:action
    expect(platformUser!.children![1].label).toBe('user:write')
    const platformBilling = tree.find((group) => group.key === 'platform:billing')
    expect(platformBilling).toBeTruthy()
  })

  it('toPermissionId:scope:resource:action 串转权限 ID,未知串返回 undefined', async () => {
    // 权限清单为模块级缓存,隔离模块重验证避免跨用例状态泄漏
    vi.resetModules()
    vi.doMock('@/api/access', () => ({
      listPermissions: vi.fn().mockResolvedValue([
        makePermission({ id: 7, scope: 'platform', resource: 'user', action: 'read' }),
      ]),
      listRoles: vi.fn(),
      createRole: vi.fn(),
      updateRole: vi.fn(),
      deleteRole: vi.fn(),
      listRolePermissions: vi.fn(),
      replaceRolePermissions: vi.fn(),
    }))
    const fresh = await import('@/views/role/useStore')
    await fresh.useStore().loadPermissions()
    expect(fresh.useStore().toPermissionId('platform:user:read')).toBe(7)
    expect(fresh.useStore().toPermissionId('platform:user:nope')).toBeUndefined()
  })

  it('save 无 id 创建 + 权限顺带设置;有 id 仅改名', async () => {
    vi.mocked(createRole).mockResolvedValue(makeRole({ id: 10 }))
    vi.mocked(replaceRolePermissions).mockResolvedValue(undefined)

    const created = await store.save({ name: '新角色', permission_ids: [1, 2] })
    expect(created).toEqual({ mode: 'create', name: '新角色' })
    expect(replaceRolePermissions).toHaveBeenCalledWith(10, [1, 2])

    vi.mocked(updateRole).mockResolvedValue(makeRole())
    const updated = await store.save({ id: 2, name: '改名', permission_ids: [] })
    expect(updated.mode).toBe('update')
    expect(updateRole).toHaveBeenCalledWith(2, { name: '改名' })
  })

  it('remove/saveRolePermissions/getRolePermissions 透传,写操作 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)

    vi.mocked(deleteRole).mockResolvedValue(undefined)
    await store.remove(3)
    expect(deleteRole).toHaveBeenCalledWith(3)

    vi.mocked(replaceRolePermissions).mockResolvedValue(undefined)
    await store.saveRolePermissions(3, [1])
    expect(replaceRolePermissions).toHaveBeenCalledWith(3, [1])

    vi.mocked(listRolePermissions).mockResolvedValue(['platform:user:read'])
    expect(await store.getRolePermissions(3)).toEqual(['platform:user:read'])

    expect(listener).toHaveBeenCalledTimes(2) // getRolePermissions 不通知
    unsubscribe()
  })

  it('角色类型映射', () => {
    expect(roleTypeMap.builtin).toEqual({ label: '内置', type: 'info' })
    expect(roleTypeMap.custom).toEqual({ label: '自定义', type: 'primary' })
  })
})
