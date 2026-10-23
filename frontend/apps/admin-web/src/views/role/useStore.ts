import { computed, ref } from 'vue'
import type { TreeOption } from 'naive-ui'
import type { TableRequestContext } from '@repo/hooks'
import {
  listPermissions,
  listRoles,
  createRole,
  updateRole,
  deleteRole,
  listRolePermissions,
  replaceRolePermissions,
  type PermissionData,
  type RoleData,
} from '@/api/access'

export type { PermissionData, RoleData }

/** 权限串格式:scope:resource:action(与后端 listRolePermissions 返回一致) */
function permissionKey(permission: PermissionData): string {
  return `${permission.scope}:${permission.resource}:${permission.action}`
}

/** 权限分组:按 scope:resource 聚合 */
interface PermissionGroup {
  /** resource 标识(scope:resource) */
  key: string
  title: string
  items: PermissionData[]
}

/** 表单模型:id 可选(新增时无);permission_ids 仅创建时生效;平台域角色固定 tenant_id=0 */
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

/** scope 文案映射(供权限分组标题) */
const scopeLabelMap: Record<string, string> = {
  platform: '平台',
  tenant: '租户',
}

/** 权限清单(模块级缓存;管理端角色固定平台域,仅含 platform 域权限) */
const permissions = ref<PermissionData[]>([])
let permissionsLoaded = false

/** 按 scope:resource 聚合的权限分组 */
const permissionGroups = computed<PermissionGroup[]>(() => {
  const map = new Map<string, PermissionData[]>()
  for (const item of permissions.value) {
    const key = `${item.scope}:${item.resource}`
    const list = map.get(key) ?? []
    list.push(item)
    map.set(key, list)
  }
  return [...map.entries()].map(([key, items]) => ({
    key,
    title: `${scopeLabelMap[items[0].scope] ?? items[0].scope} · ${items[0].resource}`,
    items,
  }))
})

/** 权限树数据:scope:resource 分组节点 + 权限叶子,供 n-tree 勾选(叶子 key 即权限ID) */
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

/** 权限串 -> 权限ID 映射,用于把角色已授权权限串转换为可勾选的权限ID */
const permissionIdMap = computed(() => {
  const map = new Map<string, number>()
  for (const item of permissions.value) map.set(permissionKey(item), item.id)
  return map
})

/** 变更监听器:写操作完成后触发,供表格 refresh 联动 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/**
 * 角色数据 store:表格请求、表单保存、权限配置均走此单例,
 * 数据来源于 /api/v1/admin/access 接口(固定平台域角色 tenant_id=0)。
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

  /** 加载权限清单(仅 platform 域,管理端角色固定为平台域角色);force=true 强制重载绕过缓存 */
  const loadPermissions = async (force = false): Promise<PermissionData[]> => {
    if (force || !permissionsLoaded) {
      permissions.value = await listPermissions('platform')
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
      // 服务端固定建平台域角色(tenant_id=0),仅传 name
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

  /** 角色已授权权限串列表(scope:resource:action) */
  const getRolePermissions = async (roleId: number): Promise<string[]> => {
    return listRolePermissions(roleId)
  }

  /** 全量替换角色权限 */
  const saveRolePermissions = async (roleId: number, permissionIds: number[]): Promise<void> => {
    await replaceRolePermissions(roleId, permissionIds)
    notify()
  }

  /** 权限串转权限ID;不在清单内的串返回 undefined(调用方须显式处理,直接丢弃会导致全量替换时静默撤销) */
  const toPermissionId = (key: string): number | undefined => permissionIdMap.value.get(key)

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
    getRolePermissions,
    saveRolePermissions,
    toPermissionId,
    subscribe,
  }
}
