import { ref } from 'vue'
import type { TableRequestContext } from '@repo/hooks'
import {
  listUsers,
  createUser,
  getUser,
  updateUser,
  deleteUser,
  type UserData,
  type UserStatus,
} from '@/api/user'

export type { UserData, UserStatus }

/** 表单模型:id 可选(新增时无);平台用户固定 tenant_id=0,无所属租户字段 */
export interface UserFormValues {
  id?: number
  username: string
  password: string
  role_ids: number[]
  email: string
  phone: string
  status: UserStatus
}

/** 状态筛选选项(含「全部」占位);后端暂不支持 status 过滤,仅过滤当前页 */
export const statusFilterOptions = [
  { label: '全部状态', value: '' },
  { label: '启用', value: 'active' },
  { label: '禁用', value: 'disabled' },
]

/** 状态文案与标签类型映射(供表格/抽屉渲染) */
export const userStatusMap: Record<
  UserStatus,
  { label: string; type: 'success' | 'error' }
> = {
  active: { label: '启用', type: 'success' },
  disabled: { label: '禁用', type: 'error' },
}

/** 弹窗表单用状态选项(不含「全部」) */
export const userStatusFormOptions = [
  { label: '启用', value: 'active' },
  { label: '禁用', value: 'disabled' },
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

/** 最近一次请求的整页数据与服务端总数,供统计卡片展示 */
const pageItems = ref<UserData[]>([])
const total = ref(0)

/**
 * 平台用户数据 store:表格请求、表单保存、状态切换、删除均走此单例,
 * 数据来源于 /api/v1/admin/users 接口(作用域固化为 tenant_id=0 平台用户)。
 */
export function useStore() {
  /** 分页 + 筛选请求(供 useTable.request);keyword/status 后端暂不支持,仅过滤当前页 */
  const request = async (params: {
    page: number
    pageSize: number
    keyword?: string
    status?: string
  }, ctx?: TableRequestContext) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    const result = await listUsers(
      {
        page: params.page,
        page_size: pageSize,
      },
      ctx?.signal,
    )
    let items = result.items
    const keyword = (params.keyword ?? '').trim().toLowerCase()
    if (keyword) {
      items = items.filter(
        (r) =>
          r.username.toLowerCase().includes(keyword) ||
          (r.email ?? '').toLowerCase().includes(keyword),
      )
    }
    if (params.status) items = items.filter((r) => r.status === params.status)
    pageItems.value = items
    total.value = result.total
    return { data: items, total: result.total }
  }

  /** 新增或更新:依据 model.id 判断;返回操作结果供调用方提示 */
  const save = async (
    model: UserFormValues,
  ): Promise<{ mode: 'create' | 'update'; name: string }> => {
    let mode: 'create' | 'update'
    if (model.id) {
      await updateUser(model.id, {
        // 密码留空则不修改
        password: model.password || undefined,
        email: model.email || undefined,
        phone: model.phone || undefined,
        status: model.status,
      })
      mode = 'update'
    } else {
      // 后端固定 tenant_id=0;role_ids 必须为平台域角色,否则 Conflict
      await createUser({
        username: model.username,
        password: model.password,
        role_ids: model.role_ids,
        email: model.email || undefined,
        phone: model.phone || undefined,
      })
      mode = 'create'
    }
    notify()
    return { mode, name: model.username }
  }

  /** 启用/禁用:active ↔ disabled 切换,返回切换后状态 */
  const toggleStatus = async (row: UserData): Promise<UserStatus> => {
    const next: UserStatus = row.status === 'active' ? 'disabled' : 'active'
    await updateUser(row.id, { status: next })
    notify()
    return next
  }

  /** 删除用户 */
  const remove = async (id: number): Promise<void> => {
    await deleteUser(id)
    notify()
  }

  /** 用户详情 */
  const getDetail = async (id: number): Promise<UserData> => {
    return getUser(id)
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { pageItems, total, request, save, toggleStatus, remove, getDetail, subscribe }
}
