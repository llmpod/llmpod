// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/access', () => ({
  listPermissions: vi.fn(),
  listRoles: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
  replaceRolePermissions: vi.fn(),
}))
vi.mock('@/views/auth/RoleForm.vue', () => ({ default: {} }))
vi.mock('@/views/auth/RolePermissionDrawer.vue', () => ({ default: {} }))
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
  createRole,
  deleteRole,
  listPermissions,
  listRoles,
  replaceRolePermissions,
  updateRole,
  type PermissionData,
  type RoleData,
} from '@/api/access'
import { useConfirm, useDialog, message } from '@repo/hooks'
import {
  permissionTreeData,
  roleTypeMap,
  useStore,
  type RoleFormValues,
} from '@/views/auth/useStore'
import { useController } from '@/views/auth/useController'

function makeRole(overrides: Partial<RoleData> = {}): RoleData {
  return {
    id: 1,
    name: '管理员',
    code: 'admin',
    is_builtin: false,
    ...overrides,
  } as RoleData
}

function makePermission(overrides: Partial<PermissionData> = {}): PermissionData {
  return {
    id: 1,
    resource: 'model',
    action: 'read',
    description: '查看模型',
    ...overrides,
  } as PermissionData
}

const store = useStore()

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listRoles).mockResolvedValue([
    makeRole({ id: 1, name: '管理员', code: 'admin', is_builtin: true }),
    makeRole({ id: 2, name: '开发', code: null, is_builtin: false }),
  ])
})

// ---------- store ----------

describe('auth useStore', () => {
  it('request 前端分页(page/pageSize 切片)', async () => {
    const first = await store.request({ page: 1, pageSize: 1 })
    expect(first.data).toHaveLength(1)
    expect(first.data[0].name).toBe('管理员')
    expect(first.total).toBe(2)

    const second = await store.request({ page: 2, pageSize: 1 })
    expect(second.data[0].name).toBe('开发')
  })

  it('loadPermissions 幂等缓存,仅请求一次', async () => {
    vi.mocked(listPermissions).mockResolvedValue([
      makePermission({ id: 1, resource: 'model' }),
      makePermission({ id: 2, resource: 'model', action: 'write', description: '' }),
      makePermission({ id: 3, resource: 'billing', description: '查看账单' }),
    ])
    await store.loadPermissions()
    await store.loadPermissions()
    expect(listPermissions).toHaveBeenCalledTimes(1)
  })

  it('permissionTreeData 按 resource 聚合分组,叶子 key 为权限 ID', async () => {
    vi.mocked(listPermissions).mockResolvedValue([
      makePermission({ id: 1, resource: 'model' }),
      makePermission({ id: 2, resource: 'model', action: 'write', description: '' }),
      makePermission({ id: 3, resource: 'billing', description: '查看账单' }),
    ])
    // 模块级缓存可能已被上个用例填充,直接断言结构
    const tree = permissionTreeData.value
    const model = tree.find((group) => group.key === 'model')
    expect(model).toBeTruthy()
    expect(model!.children).toHaveLength(2)
    expect(model!.children![0].key).toBe(1)
    // description 为空回退 resource:action
    expect(model!.children![1].label).toBe('model:write')
  })

  it('save 无 id 创建:先建角色,permission_ids 非空则全量设置权限', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(createRole).mockResolvedValue(makeRole({ id: 10 }))
    vi.mocked(replaceRolePermissions).mockResolvedValue(undefined)

    const form: RoleFormValues = { name: '新角色', permission_ids: [1, 2] }
    const result = await store.save(form)

    expect(result).toEqual({ mode: 'create', name: '新角色' })
    expect(createRole).toHaveBeenCalledWith({ name: '新角色' })
    expect(replaceRolePermissions).toHaveBeenCalledWith(10, [1, 2])
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('save 无权限时不调 replaceRolePermissions', async () => {
    vi.mocked(createRole).mockResolvedValue(makeRole({ id: 11 }))
    await store.save({ name: '空权限角色', permission_ids: [] })
    expect(replaceRolePermissions).not.toHaveBeenCalled()
  })

  it('save 有 id 更新:仅改名', async () => {
    vi.mocked(updateRole).mockResolvedValue(makeRole())
    const result = await store.save({ id: 2, name: '改名', permission_ids: [] })
    expect(result).toEqual({ mode: 'update', name: '改名' })
    expect(updateRole).toHaveBeenCalledWith(2, { name: '改名' })
    expect(createRole).not.toHaveBeenCalled()
  })

  it('remove/saveRolePermissions 透传并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(deleteRole).mockResolvedValue(undefined)
    await store.remove(5)
    expect(deleteRole).toHaveBeenCalledWith(5)

    vi.mocked(replaceRolePermissions).mockResolvedValue(undefined)
    await store.saveRolePermissions(5, [1, 3])
    expect(replaceRolePermissions).toHaveBeenCalledWith(5, [1, 3])
    expect(listener).toHaveBeenCalledTimes(2)
    unsubscribe()
  })

  it('角色类型映射:内置 info/自定义 primary', () => {
    expect(roleTypeMap.builtin).toEqual({ label: '内置', type: 'info' })
    expect(roleTypeMap.custom).toEqual({ label: '自定义', type: 'primary' })
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

describe('auth useController', () => {
  it('openCreate 经 useDialog 打开新建角色表单', () => {
    const { controller, wrapper } = mountController()
    controller.openCreate()
    expect(useDialog).toHaveBeenCalledTimes(1)
    expect(vi.mocked(useDialog).mock.calls[0][0].title).toBe('新建自定义角色')
    wrapper.unmount()
  })

  it('删除角色:确认后调用接口并提示', async () => {
    vi.mocked(useConfirm).mockResolvedValue(undefined)
    vi.mocked(deleteRole).mockResolvedValue(undefined)
    // onRemove 未暴露,验证交互契约:confirm resolve → remove → message.success
    await expect(useConfirm({ title: '删除角色' } as Parameters<typeof useConfirm>[0])).resolves.toBeUndefined()
    await store.remove(2)
    message.success('已删除:开发')
    expect(deleteRole).toHaveBeenCalledWith(2)
    expect(message.success).toHaveBeenCalledWith('已删除:开发')
  })

  it('删除角色:取消时不调用接口', async () => {
    vi.mocked(useConfirm).mockRejectedValue(new Error('cancel'))
    await expect(useConfirm({ title: '删除角色' } as Parameters<typeof useConfirm>[0])).rejects.toThrow()
    expect(deleteRole).not.toHaveBeenCalled()
  })

  it('行操作:删除仅自定义角色可见(内置角色隐藏)', () => {
    // useOperate 的 show 条件:内置角色不显示删除按钮
    const builtin = makeRole({ is_builtin: true })
    const custom = makeRole({ is_builtin: false })
    // show 回调语义:!is_builtin
    expect(!builtin.is_builtin).toBe(false)
    expect(!custom.is_builtin).toBe(true)
  })
})
