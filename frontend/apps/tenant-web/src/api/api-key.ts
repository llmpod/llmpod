import { request } from './request'

/** API Key 状态：active=启用 | disabled=已禁用（后端仅此两种） */
export type ApiKeyStatus = 'active' | 'disabled'

/** API Key 信息（对应后端 ApiKeyOut；与 admin 侧同源，租户行 tenant_id 恒为当前租户） */
export interface ApiKeyData {
  id: number
  /** 明文的 SHA-256 hex（数据面鉴权指纹，明文见 plaintext） */
  key_hash: string
  /** 所属租户ID（本端点恒为当前租户） */
  tenant_id: number | null
  /** Key 显示名 */
  name: string
  /** 允许调用的模型别名列表；空=全部模型可用（需 ⊆ 本租户可用集=发布−封禁） */
  allowed_models: string[]
  /** 每分钟请求数上限（null=不限） */
  rpm_limit: number | null
  /** 每分钟 token 上限（null=不限） */
  tpm_limit: number | null
  /** Key 生命周期总 token 配额（null=不限） */
  total_token_quota: number | null
  /** 按模型的 token 配额映射（key=模型别名，value=配额） */
  model_token_quotas: Record<string, number> | null
  /** Key 预算上限（元，Decimal 字符串；null=不限） */
  budget_limit: string | null
  /** 已真实消费（元，Decimal 字符串；只结算时累加，预扣不动） */
  budget_spent: string
  /** 在途预算冻结（元，Decimal 字符串；预扣时 +、结算/解冻时 -） */
  budget_frozen: string
  /** 状态：active | disabled */
  status: ApiKeyStatus
  /** 过期时间（ISO8601，null=永不过期） */
  expires_at: string | null
  /** 创建时间（ISO8601，带时区） */
  created_at: string
  /** 更新时间（ISO8601，带时区） */
  updated_at: string
  /** Key 明文（可逆解密回显，列表/详情随时可查） */
  plaintext: string
}

/** 签发 Key 成功响应（对应后端 ApiKeyCreatedOut）：Key 视图 + 明文 */
export interface ApiKeyCreatedData {
  /** 新建的 Key 视图（同列表项字段） */
  api_key: ApiKeyData
  /** Key 明文——与 api_key.plaintext 相同（明文持久可查，非仅此一次） */
  plaintext: string
}

/** 签发 Key 请求体（对应后端 CreateTenantKeyRequest；归属强制=登录租户，无 tenant_id 字段） */
export interface CreateTenantKeyPayload {
  /** Key 显示名（必填，1~128 字符） */
  name: string
  /** 允许调用的模型别名列表；缺省/传 [] = 全部可用模型（填了则需 ⊆ 本租户可用集=发布−封禁） */
  allowed_models?: string[]
  /** 每分钟请求数上限（>0，空=不限） */
  rpm_limit?: number | null
  /** 每分钟 token 上限（>0，空=不限） */
  tpm_limit?: number | null
  /** Key 总 token 配额（>0，空=不限） */
  total_token_quota?: number | null
  /** 按模型的 token 配额映射 */
  model_token_quotas?: Record<string, number> | null
  /** Key 预算上限（元，可传 number 或 Decimal 字符串；空=不限） */
  budget_limit?: number | string | null
  /** 过期时间（ISO8601，空=永不过期） */
  expires_at?: string | null
}

/** 更新 Key 请求体（对应后端 UpdateTenantKeyRequest；所有字段可空，不传=不改） */
export interface UpdateTenantKeyPayload {
  /** 不传则不修改 */
  name?: string | null
  /** 不传则不修改（传了会重新走可用集校验=发布−封禁；传 [] = 清空为「全部可用模型」） */
  allowed_models?: string[] | null
  /** 不传则不修改 */
  rpm_limit?: number | null
  /** 不传则不修改 */
  tpm_limit?: number | null
  /** 不传则不修改 */
  total_token_quota?: number | null
  /** 不传则不修改 */
  model_token_quotas?: Record<string, number> | null
  /** 不传则不修改 */
  budget_limit?: number | string | null
  /** 不传则不修改 */
  expires_at?: string | null
}

/** 列出本租户 API Key 查询参数（GET /api/v1/tenant/api-keys，均可空；作用域硬绑当前租户） */
export interface TenantKeyListParams {
  /** 按显示名模糊过滤（contains） */
  name?: string
  /** 按状态过滤：active | disabled */
  status?: string
}

/** 列出本租户全部 API Key（GET /api/v1/tenant/api-keys，无分页全量返回，含已禁用，id 倒序，含明文回显；name/status 服务端过滤） */
export function listTenantKeys(
  params?: TenantKeyListParams,
  signal?: AbortSignal,
): Promise<ApiKeyData[]> {
  return request({ url: '/tenant/api-keys', method: 'GET', params, signal })
}

/** 签发本租户 API Key（POST /api/v1/tenant/api-keys；明文持久可查，列表亦回显） */
export function createTenantKey(data: CreateTenantKeyPayload): Promise<ApiKeyCreatedData> {
  return request({ url: '/tenant/api-keys', method: 'POST', data })
}

/** 更新本租户 API Key（POST /api/v1/tenant/api-keys/{key_id}/update，不传的字段不改） */
export function updateTenantKey(keyId: number, data: UpdateTenantKeyPayload): Promise<ApiKeyData> {
  return request({ url: `/tenant/api-keys/${keyId}/update`, method: 'POST', data })
}

/** 禁用本租户 API Key（POST /api/v1/tenant/api-keys/{key_id}/disable；立即失效，数据面下次请求即 401，记录保留，可重新启用；幂等） */
export function disableTenantKey(keyId: number): Promise<ApiKeyData> {
  return request({ url: `/tenant/api-keys/${keyId}/disable`, method: 'POST' })
}

/** 启用本租户 API Key（POST /api/v1/tenant/api-keys/{key_id}/enable；重新启用被禁用的 Key，立即恢复可用；幂等） */
export function enableTenantKey(keyId: number): Promise<ApiKeyData> {
  return request({ url: `/tenant/api-keys/${keyId}/enable`, method: 'POST' })
}

/** 删除本租户 API Key（POST /api/v1/tenant/api-keys/{key_id}/delete；彻底删除，不可恢复；data 为 null） */
export function deleteTenantKey(keyId: number): Promise<null> {
  return request({ url: `/tenant/api-keys/${keyId}/delete`, method: 'POST' })
}

/** 重置 Key 预算累计（POST /api/v1/tenant/api-keys/{key_id}/reset-budget，budget_spent 清零，budget_limit 不动） */
export function resetTenantKeyBudget(keyId: number): Promise<ApiKeyData> {
  return request({ url: `/tenant/api-keys/${keyId}/reset-budget`, method: 'POST' })
}
