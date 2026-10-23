import { request } from './request'

/** 端点协议：openai | anthropic（一协议一条端点记录） */
export type EndpointProtocol = 'openai' | 'anthropic'

/** 端点健康状态 */
export type EndpointHealthStatus = 'healthy' | 'degraded' | 'down'

/** 来源端点信息（对应后端 ModelEndpointSchema；注意 api_key 字段被 exclude 不返回） */
export interface EndpointData {
  id: number
  /** 所属部署ID（null=外部登记端点；本地=部署投影同步） */
  deployment_id: string | null
  /** 所属实例ID（可空，指 rank-0） */
  instance_id: string | null
  /** 引擎类型：vllm | sglang | external | ... */
  engine_type: string
  /** 协议：openai | anthropic */
  protocol: EndpointProtocol
  /** 转发基址（如 https://api.openai.com/v1） */
  api_base: string
  /** 协议端点子路径（不含版本根 /v1，如 chat/completions；null=按协议约定） */
  path: string | null
  /** 上游模型名（外部=官方名 gpt-4o） */
  served_model: string
  /** 端点展示名（本地=部署名，外部=自定义） */
  name: string
  /** 供应商名（前端分组：本地集群/OpenAI/…） */
  provider_name: string | null
  /** 上下文窗口（可空） */
  context_window: number | null
  /** 支持的操作列表 */
  operations: string[]
  /** 能力标签列表 */
  capabilities: string[]
  /** 手动启用开关（false 移出候选，保留记录） */
  enabled: boolean
  /** 健康状态：healthy | degraded | down */
  health_status: EndpointHealthStatus
  /** 最近一次部署投影同步时间（外部端点恒 null） */
  last_synced_at: string | null
  /** 创建时间（ISO8601，带时区） */
  created_at: string
  /** 更新时间（ISO8601，带时区） */
  updated_at: string
}

/** 创建端点请求体（对应后端 CreateEndpointRequest；protocol/api_base/served_model/name 必填） */
export interface CreateEndpointPayload {
  /** 协议：openai | anthropic */
  protocol: EndpointProtocol
  /** 转发基址（需带版本根 /v1，如 https://api.openai.com/v1） */
  api_base: string
  /** 上游模型名（如 gpt-4o） */
  served_model: string
  /** 端点展示名 */
  name: string
  /** 外部 API Key（本地端点可空） */
  api_key?: string | null
  /** 供应商名（前端分组） */
  provider_name?: string | null
  /** 协议端点子路径（不含版本根 /v1，如 chat/completions；可空，缺省按协议约定） */
  path?: string | null
  /** 支持的操作（可空） */
  operations?: string[] | null
}

/** 调整端点请求体（对应后端 UpdateEndpointRequest；全部可空，不传=不改） */
export interface UpdateEndpointPayload {
  /** 不传则不改 */
  api_base?: string | null
  /** 不传则不改 */
  served_model?: string | null
  /** 不传则不改 */
  name?: string | null
  /** 不传则不改 */
  api_key?: string | null
  /** 不传则不改 */
  provider_name?: string | null
  /** 不传则不改 */
  path?: string | null
  /** 启用开关，不传则不改 */
  enabled?: boolean | null
  /** 健康状态，不传则不改 */
  health_status?: EndpointHealthStatus | null
}

/** 列出全部来源端点（GET /api/v1/admin/endpoints，无分页，全量返回） */
export function listEndpoints(signal?: AbortSignal): Promise<EndpointData[]> {
  return request({ url: '/admin/endpoints', method: 'GET', signal })
}

/** 登记外部端点（POST /api/v1/admin/endpoints；返回创建结果） */
export function createEndpoint(data: CreateEndpointPayload): Promise<unknown> {
  return request({ url: '/admin/endpoints', method: 'POST', data })
}

/** 调整端点（POST /api/v1/admin/endpoints/{endpoint_id}/update，不传的字段不改） */
export function updateEndpoint(
  endpointId: number,
  data: UpdateEndpointPayload,
): Promise<EndpointData> {
  return request({ url: `/admin/endpoints/${endpointId}/update`, method: 'POST', data })
}

/** 删除端点（POST /api/v1/admin/endpoints/{endpoint_id}/delete） */
export function deleteEndpoint(endpointId: number): Promise<unknown> {
  return request({ url: `/admin/endpoints/${endpointId}/delete`, method: 'POST' })
}