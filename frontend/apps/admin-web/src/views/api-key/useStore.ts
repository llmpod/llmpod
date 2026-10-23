import type { TableRequestContext } from '@repo/hooks'
import {
  listApiKeys,
  createApiKey,
  disableApiKey,
  enableApiKey,
  deleteApiKey,
  updateApiKey,
  resetApiKeyBudget,
  type ApiKeyData,
  type ApiKeyCreatedData,
  type ApiKeyStatus,
  type CreateApiKeyPayload,
  type UpdateApiKeyPayload,
} from '@/api/api-key'

export type { ApiKeyData, ApiKeyCreatedData, ApiKeyStatus, CreateApiKeyPayload, UpdateApiKeyPayload }

/** 表单模型:id 可选(新增时无) */
export interface ApiKeyFormValues {
  id?: number
  /** 所属租户ID（null=平台直发 Key） */
  tenant_id: number | null
  /** Key 显示名 */
  name: string
  /** 允许调用的模型别名列表 */
  allowed_models: string[]
  /** 每分钟请求数上限（null=不限） */
  rpm_limit: number | null
  /** 每分钟 token 上限（null=不限） */
  tpm_limit: number | null
  /** Key 总 token 配额（null=不限） */
  total_token_quota: number | null
  /** Key 预算上限（元，空串=不限） */
  budget_limit: string
  /** 过期时间（本地时间字符串 yyyy-MM-ddTHH:mm:ss，null=永不过期；提交时转 UTC ISO） */
  expires_at: string | null
}

/** 状态筛选选项(含「全部」占位) */
export const statusFilterOptions = [
  { label: '全部', value: '' },
  { label: '启用', value: 'active' },
  { label: '已禁用', value: 'disabled' },
]

/** 状态文案与标签类型映射 */
export const apiKeyStatusMap: Record<
  ApiKeyStatus,
  { label: string; type: 'success' | 'error' }
> = {
  active: { label: '启用', type: 'success' },
  disabled: { label: '已禁用', type: 'error' },
}

/** 高级限流字段任一有值（编辑回填）→ 表单高级区默认展开，避免已设值不可见 */
export function hasAdvancedLimits(
  initial?: Pick<ApiKeyData, 'rpm_limit' | 'tpm_limit' | 'total_token_quota' | 'budget_limit'> | null,
): boolean {
  if (!initial) return false
  return !!(
    initial.rpm_limit ||
    initial.tpm_limit ||
    initial.total_token_quota ||
    initial.budget_limit
  )
}

/** Decimal 字符串去尾零展示("12.500000" → "12.5","0.0000005" → "0.0000005" 不进科学计数法) */
export { formatDecimal } from '@repo/hooks'

/** 本地时间字符串(yyyy-MM-ddTHH:mm:ss,表单值) → UTC ISO8601;null/空串/非法返回 null */
export function localInputToIso(value: string | null): string | null {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  return d.toISOString()
}

/** UTC ISO8601 → 本地时间字符串(yyyy-MM-ddTHH:mm:ss,供日期选择器回填);空值返回 null
 *  (date-picker 空值契约是 null,空串会触发 naive-ui RangeError: Invalid time value) */
export function isoToLocalInput(value: string | null): string | null {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** ISO8601 → YYYY-MM-DD HH:mm 展示（本地时区）;空值显示 — */
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

/**
 * API Key 数据 store:表格请求、创建、禁用/启用、调整、删除、重置预算均走此单例,
 * 数据来源于 /api/v1/admin/api-keys 接口（全量返回，前端 local 模式分页）。
 */
export function useStore() {
  /** 全量请求(供 useTable.request，local 模式前端分页+筛选)
   *  GET /admin/api-keys 无分页参数，全量返回；status 在此前端过滤 */
  const request = async (
    params: { status?: string; keyword?: string },
    ctx?: TableRequestContext,
  ) => {
    let list = await listApiKeys(ctx?.signal)
    // 状态筛选（local 模式不自动处理，在此过滤）
    if (params.status) list = list.filter((r) => r.status === params.status)
    return { data: list, total: list.length }
  }

  /** 创建 Key:返回创建结果（含明文；明文持久可查，列表亦回显） */
  const create = async (model: ApiKeyFormValues): Promise<ApiKeyCreatedData> => {
    const result = await createApiKey({
      name: model.name,
      tenant_id: model.tenant_id,
      // 空=全部模型可用（后端语义，无需转 undefined）
      allowed_models: model.allowed_models,
      rpm_limit: model.rpm_limit,
      tpm_limit: model.tpm_limit,
      total_token_quota: model.total_token_quota,
      budget_limit: model.budget_limit || null,
      expires_at: localInputToIso(model.expires_at),
    })
    notify()
    return result
  }

  /** 调整 Key:表单即最终状态,显式传值(空串/null=清除该项,空数组=清空为「全部模型可用」) */
  const update = async (model: ApiKeyFormValues): Promise<{ name: string }> => {
    if (!model.id) throw new Error('缺少 Key ID')
    await updateApiKey(model.id, {
      name: model.name,
      allowed_models: model.allowed_models,
      rpm_limit: model.rpm_limit,
      tpm_limit: model.tpm_limit,
      total_token_quota: model.total_token_quota,
      budget_limit: model.budget_limit || null,
      expires_at: localInputToIso(model.expires_at),
    })
    notify()
    return { name: model.name }
  }

  /** 禁用 Key(立即失效,数据面下次请求即 401;记录保留,可重新启用) */
  const disable = async (id: number): Promise<void> => {
    await disableApiKey(id)
    notify()
  }

  /** 启用 Key(重新启用被禁用的 Key,立即恢复可用) */
  const enable = async (id: number): Promise<void> => {
    await enableApiKey(id)
    notify()
  }

  /** 删除 Key(彻底删除,不可恢复) */
  const remove = async (id: number): Promise<void> => {
    await deleteApiKey(id)
    notify()
  }

  /** 重置预算累计(清零 budget_spent) */
  const resetBudget = async (id: number): Promise<void> => {
    await resetApiKeyBudget(id)
    notify()
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { request, create, update, disable, enable, remove, resetBudget, subscribe }
}