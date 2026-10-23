import { request } from './request'

/** 权限信息（对应后端 PermissionSchema，固定 scope=tenant） */
export interface PermissionData {
  id: number
  /** 权限域：tenant=租户端 */
  scope: string
  /** 资源标识，如 api-key / team */
  resource: string
  /** 操作标识，如 read / create / update / delete */
  action: string
  /** 权限说明（可空） */
  description: string | null
}

/** 角色信息（对应后端 RoleDetailOut） */
export interface RoleData {
  id: number
  /** 所属租户ID */
  tenant_id: number
  /** 角色名称（展示用） */
  name: string
  /** 角色编码（仅内置角色有值，自定义角色为 null） */
  code: string | null
  /** 是否内置角色（内置角色不可删除） */
  is_builtin: boolean
  /** 已授权的权限ID列表（后端装配，无关联时为 []） */
  permission_ids: number[]
}

/** 列出租户域权限（GET /api/v1/tenant/access/permissions，固定 scope=tenant） */
export function listPermissions(): Promise<PermissionData[]> {
  return request({ url: '/tenant/access/permissions', method: 'GET' })
}

/** 列出本租户角色（GET /api/v1/tenant/access/roles） */
export function listRoles(signal?: AbortSignal): Promise<RoleData[]> {
  return request({ url: '/tenant/access/roles', method: 'GET', signal })
}

/** 创建自定义角色（POST /api/v1/tenant/access/roles，仅传 name） */
export function createRole(data: { name: string }): Promise<RoleData> {
  return request({ url: '/tenant/access/roles', method: 'POST', data })
}

/** 更新角色（POST /api/v1/tenant/access/roles/{role_id}/update，仅支持改名） */
export function updateRole(roleId: number, data: { name?: string }): Promise<RoleData> {
  return request({ url: `/tenant/access/roles/${roleId}/update`, method: 'POST', data })
}

/** 删除自定义角色（POST /api/v1/tenant/access/roles/{role_id}/delete，内置角色不可删） */
export function deleteRole(roleId: number): Promise<unknown> {
  return request({ url: `/tenant/access/roles/${roleId}/delete`, method: 'POST' })
}

/** 全量替换角色权限（POST /api/v1/tenant/access/roles/{role_id}/permissions，permission_ids 即目标全集） */
export function replaceRolePermissions(
  roleId: number,
  permissionIds: number[],
): Promise<unknown> {
  return request({
    url: `/tenant/access/roles/${roleId}/permissions`,
    method: 'POST',
    data: { permission_ids: permissionIds },
  })
}
