import { ref } from 'vue'
import type { TableRequestContext } from '@repo/hooks'
import {
  listTenantUsers,
  createTenantUser,
  updateTenantUser,
  deleteTenantUser,
  getTenantUser,
  type CreateTenantUserPayload,
  type UpdateTenantUserPayload,
  type TenantUserData,
  type MemberStatus,
} from '@/api/team'
import { listRoles } from '@/api/access'

export type { TenantUserData, MemberStatus }

/** 列表行 / 详情记录 */
export type Member = TenantUserData

/** 状态文案与标签类型映射(供表格/抽屉渲染) */
export const memberStatusMap: Record<MemberStatus, { label: string; type: 'success' | 'error' }> =
  {
    active: { label: '启用', type: 'success' },
    disabled: { label: '已禁用', type: 'error' },
  }

/** 状态筛选选项(含「全部成员」);后端列表无过滤参数,在前端过滤 */
export const statusFilterOptions = [
  { label: '全部成员', value: '' },
  { label: '启用', value: 'active' },
  { label: '已禁用', value: 'disabled' },
]

/** ISO8601 时间格式化为 YYYY-MM-DD HH:mm;空值显示 — */
export function formatDateTime(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 变更监听器:写操作完成后触发,供表格 refresh 联动 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/** 最近一次请求的全量成员,供统计卡片与详情抽屉联动 */
const records = ref<TenantUserData[]>([])

/** 角色ID -> 角色名称映射(随每次请求刷新,供表格/抽屉展示角色名) */
const roleNameMap = ref<Map<number, string>>(new Map())

/**
 * 团队成员数据 store:表格请求、状态切换、删除均走此单例,
 * 数据来源于 /api/v1/tenant/users 接口;列表接口全量返回,
 * 状态筛选在前端完成,用户名/邮箱搜索由 useTable local filter 处理。
 */
export function useStore() {
  /** 全量请求(供 useTable.request,local 模式不带分页参数);status 前端过滤 */
  const request = async (params: Record<string, unknown>, ctx?: TableRequestContext) => {
    const [data, roles] = await Promise.all([listTenantUsers(ctx?.signal), listRoles(ctx?.signal)])
    records.value = data
    roleNameMap.value = new Map(roles.map((role) => [role.id, role.name]))
    const status = params.status as MemberStatus | '' | undefined
    const filtered = status ? data.filter((r) => r.status === status) : data
    return { data: filtered, total: filtered.length }
  }

  /** 角色名称展示:未知名称回退为 #id */
  const roleName = (id: number): string => roleNameMap.value.get(id) ?? `#${id}`

  /** 创建成员(POST /api/v1/tenant/users;role_ids 必须属当前租户) */
  const createMember = async (payload: CreateTenantUserPayload): Promise<void> => {
    await createTenantUser(payload)
    notify()
  }

  /** 编辑成员(POST /{id}/update;username 不可改,密码传入则重置,未传字段不修改) */
  const updateMember = async (id: number, payload: UpdateTenantUserPayload): Promise<void> => {
    await updateTenantUser(id, payload)
    notify()
  }

  /** 启用/禁用:active ↔ disabled 切换,返回切换后状态 */
  const toggleStatus = async (row: Member): Promise<MemberStatus> => {
    const next: MemberStatus = row.status === 'active' ? 'disabled' : 'active'
    await updateTenantUser(row.id, { status: next })
    notify()
    return next
  }

  /** 移除成员(后端级联清理其角色关联;管理员角色最后一名持有者会被拒绝) */
  const removeMember = async (id: number): Promise<void> => {
    await deleteTenantUser(id)
    notify()
  }

  /** 成员详情 */
  const getDetail = async (id: number): Promise<Member> => {
    return getTenantUser(id)
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return {
    records,
    request,
    roleName,
    createMember,
    updateMember,
    toggleStatus,
    removeMember,
    getDetail,
    subscribe,
  }
}
