import { request } from './request'

/** 调用日志状态：success=成功 | error=失败 | interrupted=中断 */
export type RequestLogStatus = 'success' | 'error' | 'interrupted'

/** 上游流向：local=本地部署 | external=外部供应商（null=未尝试转发，如无候选端点） */
export type RequestLogUpstreamType = 'local' | 'external'

/** 调用日志列表项（对应后端 RequestLogOut，租户视图；provider_name/endpoint_id 仅管理端可见） */
export interface RequestLogData {
  id: number
  /** 请求唯一ID（uuid） */
  request_id: string
  /** 租户ID（本端点恒为当前租户） */
  tenant_id: number | null
  /** API Key 行ID */
  api_key_id: number
  /** 对外模型别名 */
  model_alias: string
  /** 引擎内部模型名（可空） */
  served_model: string | null
  /** 操作：chat.completions | anthropic.messages | embeddings | rerank */
  operation: string
  /** 协议：openai | anthropic（可空） */
  protocol: string | null
  /** 引擎类型（vllm/sglang/external/…，可空） */
  engine: string | null
  /** 上游流向：local=本地部署 | external=外部供应商（null=未尝试转发，如无候选端点） */
  upstream_type: RequestLogUpstreamType | null
  /** failover 重试次数（首试成功=0，= 尝试次数−1） */
  retry_count: number
  /** 输入 token */
  prompt_tokens: number
  /** 输出 token */
  completion_tokens: number
  /** 总 token */
  total_tokens: number
  /** 缓存命中 token（prompt 子集；null=引擎未上报，0=明确无命中） */
  cached_tokens: number | null
  /** 费用（usage × 定价，Decimal 字符串；可空） */
  cost: string | null
  /** 端到端延迟（毫秒，可空） */
  latency_ms: number | null
  /** 状态：success | error | interrupted */
  status: RequestLogStatus
  /** 响应 HTTP 状态码（可空） */
  http_status: number | null
  /** 请求体块哈希（BodyStore，可空） */
  request_ref: string | null
  /** 响应体块哈希（可空） */
  response_ref: string | null
  /** 会话前缀指纹（协议感知；无会话语义为空，可空） */
  conv_prefix_hash: string | null
  /** body 是否被截断（默认 32KB/字段） */
  truncated: boolean
  /** 快照是否关闭（true=仅记计量，无 body） */
  snapshot_disabled: boolean
  /** 失败原因（status=error 时，可空） */
  error_message: string | null
  /** 请求时间（ISO8601） */
  created_at: string
}

/** 调用日志详情（对应后端 RequestLogDetailOut = 列表字段 + 请求/响应 body；块缺失时为 null） */
export interface RequestLogDetailData extends RequestLogData {
  /** 请求体（BodyStore 块还原为 JSON；非 JSON 时可能为 { _raw: "..." }，可空） */
  request_body: unknown | null
  /** 响应体快照（BodyStore 块还原；非 JSON 时可能为 { _raw: "..." }，可空） */
  response_body: unknown | null
}

/** 调用日志分页数据（对应后端 PageData[RequestLogOut]） */
export interface RequestLogPageData {
  items: RequestLogData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 列出本租户调用日志查询参数（均可空；api_key_id 仅接受本租户的 Key） */
export interface ListTenantRequestLogsParams {
  /** 页码，从 1 开始，默认 1 */
  page?: number
  /** 每页条数，1~100，默认 20 */
  page_size?: number
  /** 按 API Key ID 筛选（仅限本租户的 Key，别人的 Key id 结果恒空） */
  api_key_id?: number | null
  /** 按对外模型名筛选（可空） */
  model?: string | null
  /** 按状态筛选：success | error | interrupted（可空） */
  status?: RequestLogStatus | null
  /** 按会话前缀指纹筛选（可空） */
  conv_prefix_hash?: string | null
  /** 起始时间（毫秒时间戳，含；可空） */
  since_ms?: number | null
  /** 截止时间（毫秒时间戳，含；可空） */
  until_ms?: number | null
}

/** 列出本租户调用日志（GET /api/v1/tenant/request-logs，分页；跨租户数据不可见） */
export function listTenantRequestLogs(
  params: ListTenantRequestLogsParams,
  signal?: AbortSignal,
): Promise<RequestLogPageData> {
  return request({ url: '/tenant/request-logs', method: 'GET', params, signal })
}

/** 本租户调用日志详情（GET /api/v1/tenant/request-logs/{request_id}，含请求/响应 body；跨租户视同不存在 404） */
export function getTenantRequestLog(
  requestId: string,
  signal?: AbortSignal,
): Promise<RequestLogDetailData> {
  return request({ url: `/tenant/request-logs/${requestId}`, method: 'GET', signal })
}
