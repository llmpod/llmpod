import { computed, ref } from 'vue'
import type { TreeOption } from 'naive-ui'
import type { TableRequestContext } from '@repo/hooks'
import {
  listPermissions,
  listRoles,
  createRole,
  updateRole,
  deleteRole,
  replaceRolePermissions,
  type PermissionData,
  type RoleData,
} from '@/api/access'

export type { PermissionData, RoleData }

/** 权限分组:按 resource 聚合(权限接口固定 scope=tenant) */
interface PermissionGroup {
  /** resource 标识 */
  key: string
  title: string
  items: PermissionData[]
}

/** 表单模型:id 可选(新增时无);permission_ids 仅创建时生效 */
export interface RoleFormValues {
  id?: number
  name: string
  permission_ids: number[]
}

/** 类型文案与标签类型映射(供表格渲染) */
export const roleTypeMap: Record<string, { label: string; type: 'info' | 'primary' }> = {
  builtin: { label: '内置', type: 'info' },
  custom: { label: '自定义', type: 'primary' },
}

/** 权限清单(模块级缓存,租户域权限集合稳定,无需重复拉取) */
const permissions = ref<PermissionData[]>([])
let permissionsLoaded = false

/** 按 resource 聚合的权限分组 */
const permissionGroups = computed<PermissionGroup[]>(() => {
  const map = new Map<string, PermissionData[]>()
  for (const item of permissions.value) {
    const list = map.get(item.resource) ?? []
    list.push(item)
    map.set(item.resource, list)
  }
  return [...map.entries()].map(([key, items]) => ({
    key,
    title: key,
    items,
  }))
})

/** 权限树数据:resource 分组节点 + 权限叶子,供 n-tree 勾选(叶子 key 即权限ID) */
export const permissionTreeData = computed<TreeOption[]>(() =>
  permissionGroups.value.map((group) => ({
    key: group.key,
    label: group.title,
    children: group.items.map((item) => ({
      key: item.id,
      label: item.description || `${item.resource}:${item.action}`,
    })),
  })),
)

/** 变更监听器:写操作完成后触发,供表格 refresh 联动 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/**
 * 角色数据 store:表格请求、表单保存、权限配置均走此单例,
 * 数据来源于 /api/v1/tenant/access 接口(当前租户域)。
 */
export function useStore() {
  /** 分页请求(供 useTable.request);后端返回全量列表,此处前端分页 */
  const request = async (params: { page: number; pageSize: number }, ctx?: TableRequestContext) => {
    const roles = await listRoles(ctx?.signal)
    const start = (params.page - 1) * params.pageSize
    return {
      data: roles.slice(start, start + params.pageSize),
      total: roles.length,
    }
  }

  /** 加载权限清单;已加载则直接返回 */
  const loadPermissions = async (): Promise<PermissionData[]> => {
    if (!permissionsLoaded) {
      permissions.value = await listPermissions()
      permissionsLoaded = true
    }
    return permissions.value
  }

  /** 新增或更新:依据 model.id 判断;创建后可顺带全量设置权限 */
  const save = async (
    model: RoleFormValues,
  ): Promise<{ mode: 'create' | 'update'; name: string }> => {
    let mode: 'create' | 'update'
    if (model.id) {
      // 后端仅支持改名
      await updateRole(model.id, { name: model.name })
      mode = 'update'
    } else {
      const role = await createRole({ name: model.name })
      if (model.permission_ids.length > 0) {
        await replaceRolePermissions(role.id, model.permission_ids)
      }
      mode = 'create'
    }
    notify()
    return { mode, name: model.name }
  }

  /** 删除自定义角色(内置角色由后端守卫拒绝) */
  const remove = async (id: number): Promise<void> => {
    await deleteRole(id)
    notify()
  }

  /** 全量替换角色权限 */
  const saveRolePermissions = async (roleId: number, permissionIds: number[]): Promise<void> => {
    await replaceRolePermissions(roleId, permissionIds)
    notify()
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return {
    request,
    loadPermissions,
    save,
    remove,
    saveRolePermissions,
    subscribe,
  }
}
