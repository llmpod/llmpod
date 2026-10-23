import { request } from './request'

/** 权限信息（对应后端 PermissionSchema） */
export interface PermissionData {
  id: number
  /** 权限域：platform=平台/管理端 | tenant=租户端 */
  scope: string
  /** 资源标识，如 cluster / user / gpu */
  resource: string
  /** 操作标识，如 read / create / update / delete */
  action: string
  /** 权限说明（可空） */
  description: string | null
}

/** 角色信息（对应后端 RoleSchema） */
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
  /** 已授权的权限ID列表（后端已降级，无关联时为 []，不会缺字段） */
  permission_ids: number[]
}

/** 列出权限（GET /api/v1/admin/access/permissions；不传 scope 默认只列 platform） */
export function listPermissions(scope?: string): Promise<PermissionData[]> {
  return request({ url: '/admin/access/permissions', method: 'GET', params: { scope } })
}

/** 列出角色（GET /api/v1/admin/access/roles；固定只返回平台域角色 tenant_id=0，不再返回租户端角色） */
export function listRoles(signal?: AbortSignal): Promise<RoleData[]> {
  return request({ url: '/admin/access/roles', method: 'GET', signal })
}

/** 创建自定义角色请求体（仅传 name；服务端固定建平台域角色） */
export interface CreateRolePayload {
  name: string
}

/** 创建自定义角色（POST /api/v1/admin/access/roles，服务端固定 tenant_id=0） */
export function createRole(data: CreateRolePayload): Promise<RoleData> {
  return request({ url: '/admin/access/roles', method: 'POST', data })
}

/** 更新角色（POST /api/v1/admin/access/roles/{role_id}/update，仅支持改名） */
export function updateRole(roleId: number, data: { name?: string }): Promise<RoleData> {
  return request({ url: `/admin/access/roles/${roleId}/update`, method: 'POST', data })
}

/** 删除自定义角色（POST /api/v1/admin/access/roles/{role_id}/delete，内置角色不可删） */
export function deleteRole(roleId: number): Promise<unknown> {
  return request({ url: `/admin/access/roles/${roleId}/delete`, method: 'POST' })
}

/** 列出角色已授权权限串（GET /api/v1/admin/access/roles/{role_id}/permissions，格式 scope:resource:action） */
export function listRolePermissions(roleId: number): Promise<string[]> {
  return request({ url: `/admin/access/roles/${roleId}/permissions`, method: 'GET' })
}

/** 全量替换角色权限（POST /api/v1/admin/access/roles/{role_id}/permissions，permission_ids 即目标全集） */
export function replaceRolePermissions(
  roleId: number,
  permissionIds: number[],
): Promise<unknown> {
  return request({
    url: `/admin/access/roles/${roleId}/permissions`,
    method: 'POST',
    data: { permission_ids: permissionIds },
  })
}
