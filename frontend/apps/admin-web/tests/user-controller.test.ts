// @vitest-environment happy-dom
import { flushPromises } from '@vue/test-utils'
import { createApp, h } from 'vue'
import type { VNode } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DataTableColumn } from 'naive-ui'

vi.mock('@/api/user', () => ({
  listUsers: vi.fn(),
  createUser: vi.fn(),
  getUser: vi.fn(),
  updateUser: vi.fn(),
  deleteUser: vi.fn(),
}))
vi.mock('@/api/access', () => ({ listRoles: vi.fn() }))

// useTable 替换为桩以捕获列定义
const useTableMock = vi.fn()

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useTable: (...args: unknown[]) => useTableMock(...args),
    useDialog: vi.fn(),
    useEntryAction: vi.fn(),
  }
})

import { listRoles } from '@/api/access'
import type { RoleData } from '@/api/access'
import type { UserData } from '@/api/user'
import { useController } from '@/views/user/useController'

function makeRole(id: number, name: string): RoleData {
  return { id, tenant_id: 0, name, code: null, is_builtin: false, permission_ids: [] }
}

/** 在真实组件 setup 内创建控制器(触发 onMounted/onBeforeUnmount 生命周期) */
function mountController() {
  let controller!: ReturnType<typeof useController>
  const app = createApp({
    setup: () => {
      controller = useController()
      return () => h('div')
    },
  })
  app.mount(document.createElement('div'))
  return { controller, unmount: () => app.unmount() }
}

/** useTable 收到的 options(列定义断言入口) */
function tableOptions() {
  expect(useTableMock).toHaveBeenCalledTimes(1)
  return useTableMock.mock.calls[0][0] as {
    columns: Array<DataTableColumn<UserData>>
  }
}

/** 角色列 render 输出的标签文本列表 */
function roleTagTexts(vnode: VNode): string[] {
  const tags = vnode.children as VNode[]
  return tags.map((tag) => (tag.children as { default: () => string }).default())
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listRoles).mockResolvedValue([makeRole(1, '超级管理员'), makeRole(2, '运维')])
  useTableMock.mockReturnValue({
    LmTable: {},
    LmPage: {},
    LmRefresh: {},
    LmColumn: {},
    LmSearch: {},
    refresh: vi.fn(),
    reset: vi.fn(),
    hasActiveQuery: { value: false },
    data: { value: [] },
    loading: { value: false },
  })
})

describe('user useController 表格列', () => {
  it('角色列展示角色名称标签,未知角色 ID 显示 #id', async () => {
    const { unmount } = mountController()
    await flushPromises()
    const roleCol = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).key === 'role_ids',
    ) as unknown as { title: string; render: (row: UserData) => VNode }
    expect(roleCol).toBeTruthy()
    expect(roleCol.title).toBe('角色')

    const vnode = roleCol.render({ role_ids: [1, 2, 99] } as UserData)
    expect(roleTagTexts(vnode)).toEqual(['超级管理员', '运维', '#99'])
    unmount()
  })

  it('无角色时显示 —', async () => {
    const { unmount } = mountController()
    await flushPromises()
    const roleCol = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).key === 'role_ids',
    ) as unknown as { render: (row: UserData) => VNode | string }
    expect(roleCol.render({ role_ids: [] } as UserData)).toBe('—')
    unmount()
  })

  it('挂载时拉取角色清单建立 ID→名称映射', async () => {
    const { unmount } = mountController()
    await flushPromises()
    expect(listRoles).toHaveBeenCalledTimes(1)
    unmount()
  })
})
