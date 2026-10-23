// @vitest-environment happy-dom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { loadPermissionsMock, getRolePermissionsMock, saveRolePermissionsMock } = vi.hoisted(() => ({
  loadPermissionsMock: vi.fn(),
  getRolePermissionsMock: vi.fn(),
  saveRolePermissionsMock: vi.fn(),
}))

vi.mock('@/api/access', () => ({
  listPermissions: vi.fn(),
  listRoles: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
  listRolePermissions: vi.fn(),
  replaceRolePermissions: vi.fn(),
}))
vi.mock('@/views/role/useStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/views/role/useStore')>()
  return {
    ...actual,
    permissionTreeData: actual.permissionTreeData, // 模块级 computed 保留(数据来自 mock 的 store)
    useStore: () => ({
      loadPermissions: loadPermissionsMock,
      getRolePermissions: getRolePermissionsMock,
      saveRolePermissions: saveRolePermissionsMock,
      toPermissionId: (key: string) => {
        const map: Record<string, number> = { 'platform:user:read': 1, 'platform:user:write': 2 }
        return map[key]
      },
    }),
  }
})
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return { ...actual, message: { error: vi.fn(), success: vi.fn() }, useConfirm: vi.fn() }
})

import type { RoleData } from '@/api/access'
import RolePermissionDrawer from '@/views/role/RolePermissionDrawer.vue'

function makeRole(overrides: Partial<RoleData> = {}): RoleData {
  return { id: 1, tenant_id: 0, name: '运维', code: null, is_builtin: false, permission_ids: [], ...overrides }
}

async function mountDrawer() {
  const wrapper = mount(RolePermissionDrawer, {
    props: { show: false, role: makeRole() },
    attachTo: document.body,
  })
  await wrapper.setProps({ show: true })
  await flushPromises()
  return wrapper
}

function saveButton(wrapper: ReturnType<typeof mountDrawer> extends Promise<infer W> ? W : never) {
  // footer 的保存按钮(主按钮):模板中最后一个 n-button
  const buttons = document.body.querySelectorAll('.n-drawer .n-button')
  return buttons[buttons.length - 1] as HTMLButtonElement
}

beforeEach(() => {
  vi.clearAllMocks()
  loadPermissionsMock.mockResolvedValue([])
})

describe('RolePermissionDrawer 加载失败防线(AC-02)', () => {
  it('权限加载失败:保存禁用,呈现错误占位与重新加载', async () => {
    loadPermissionsMock.mockRejectedValue(new Error('network down'))
    getRolePermissionsMock.mockResolvedValue(['platform:user:read'])
    const wrapper = await mountDrawer()

    const text = document.body.textContent ?? ''
    expect(text).toContain('权限加载失败')
    expect(text).toContain('重新加载')
    expect(saveButton(wrapper).disabled).toBe(true)

    // 直接调 onSave 也不落库(loadError 拦截,防空集全量替换)
    ;(wrapper.vm as unknown as { onSave: () => Promise<void> }).onSave()
    await flushPromises()
    expect(saveRolePermissionsMock).not.toHaveBeenCalled()

    wrapper.unmount()
    document.body.innerHTML = ''
  })

  it('重新加载成功后:错误占位消失,保存恢复可用并正常落库', async () => {
    loadPermissionsMock.mockRejectedValueOnce(new Error('timeout'))
    getRolePermissionsMock.mockResolvedValue(['platform:user:read'])
    const wrapper = await mountDrawer()
    expect(saveButton(wrapper).disabled).toBe(true)

    // 重新加载:本次成功,勾选回显
    loadPermissionsMock.mockResolvedValue([])
    const retry = [...document.body.querySelectorAll('.n-button')].find((b) =>
      b.textContent?.includes('重新加载'),
    ) as HTMLButtonElement
    retry.click()
    await flushPromises()

    expect((document.body.textContent ?? '').includes('权限加载失败')).toBe(false)
    expect(saveButton(wrapper).disabled).toBe(false)

    saveRolePermissionsMock.mockResolvedValue(undefined)
    saveButton(wrapper).click()
    await flushPromises()
    // 勾选集 [1] 正常写回,不是空集
    expect(saveRolePermissionsMock).toHaveBeenCalledWith(1, [1])

    wrapper.unmount()
    document.body.innerHTML = ''
  })

  it('加载成功路径不受影响:保存可用且写入勾选集', async () => {
    getRolePermissionsMock.mockResolvedValue(['platform:user:read', 'platform:user:write'])
    const wrapper = await mountDrawer()

    expect((document.body.textContent ?? '').includes('权限加载失败')).toBe(false)
    expect(saveButton(wrapper).disabled).toBe(false)

    saveRolePermissionsMock.mockResolvedValue(undefined)
    saveButton(wrapper).click()
    await flushPromises()
    expect(saveRolePermissionsMock).toHaveBeenCalledWith(1, [1, 2])

    wrapper.unmount()
    document.body.innerHTML = ''
  })
})
