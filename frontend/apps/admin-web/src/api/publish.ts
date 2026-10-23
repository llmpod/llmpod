import { request } from './request'

/** 路由发布状态：published=已发布 | unpublished=已下架 */
export type RouteStatus = 'published' | 'unpublished'

/** 路由创建来源：auto=部署自动发布（随部署删除）| manual=管理面手工建 */
export type RouteOrigin = 'auto' | 'manual'

/** 模型类别（对应后端 model_type；缺省 llm） */
export type ModelType = 'llm' | 'multimodal' | 'embedding' | 'rerank' | 'image' | 'stt' | 'tts'

/** 路由信息（对应后端 RouteSchema；对外模型名 + 类型/描述 + 定价 + failover） */
export interface RouteData {
  id: number
  /** 对外模型名（全局唯一） */
  name: string
  /** 发布状态：published | unpublished */
  status: RouteStatus
  /** 创建来源：auto=部署自动发布 | manual=管理面手工建 */
  origin: RouteOrigin
  /** 模型类别 */
  model_type: ModelType
  /** 模型描述（模型中心展示，可空） */
  description: string | null
  /** 最大上下文长度（tokens；部署自动发布时带入 topology.max_model_len，管理面可改，可空） */
  context_length: number | null
  /** 能力标签列表（受控词表：思考/工具调用/视觉/图像生成/联网搜索；纯展示，系统不处理） */
  tags: string[]
  /** 输入 token 定价（每百万 token；chat 输入与 embeddings/rerank 全量均按此计，空=不计费，Decimal 字符串） */
  input_price_per_mtok: string | null
  /** 输出 token 定价（每百万 token，仅生成类，空=不计费，Decimal 字符串） */
  output_price_per_mtok: string | null
  /** 缓存命中输入定价（每百万，Decimal 字符串；null=未配置，按全价计） */
  cached_input_price_per_mtok: string | null
  /** 选中端点失败后是否切换到同协议其他成员 */
  failover_enabled: boolean
  /** failover 切换次数上限（0=不切换） */
  failover_max_retries: number
  /** 创建时间（ISO8601，带时区） */
  created_at: string
  /** 更新时间（ISO8601，带时区） */
  updated_at: string
}

/** 路由成员关系（对应后端 RouteBindingSchema） */
export interface RouteBindingData {
  id: number
  /** 所属路由ID */
  route_id: number
  /** 端点ID（→ model_endpoint.id） */
  endpoint_id: number
  /** 协议内归一权重（加权随机） */
  weight: number
  /** 优先级（大者先选，故障降级） */
  priority: number
  /** 成员开关（false 移出候选） */
  enabled: boolean
  /** 创建时间（ISO8601，带时区） */
  created_at: string
  /** 更新时间（ISO8601，带时区） */
  updated_at: string
}

/** 对外服务视图：路由 + 成员列表（对应后端 PublishOut） */
export interface PublishData {
  /** 路由信息 */
  route: RouteData
  /** 成员绑定列表 */
  bindings: RouteBindingData[]
}

/** 创建路由时随请求挂上的初始成员（对应后端 CreateBindingSpec；endpoint_id 必填，其余缺省 weight=1/priority=0） */
export interface CreateBindingSpec {
  /** 端点ID（→ model_endpoint.id） */
  endpoint_id: number
  /** 协议内权重（≥1，缺省 1） */
  weight?: number | null
  /** 优先级（缺省 0） */
  priority?: number | null
}

/** 手动创建发布路由请求体（对应后端 CreateRouteRequest；name 必填，其余可选）
 *  创建后默认 status="published"（即建即上架）；name 重复返回 409；
 *  bindings 端点不存在不阻断创建（绑定在快照装配时自然无候选） */
export interface CreateRoutePayload {
  /** 对外模型名（client 请求的 model 字段），全局唯一，1~255 字符 */
  name: string
  /** 模型类别（默认 llm） */
  model_type?: ModelType
  /** 模型描述（模型中心展示，可空） */
  description?: string | null
  /** 最大上下文长度（tokens，>0；空=未设置） */
  context_length?: number | null
  /** 能力标签（受控词表，纯展示；默认 []） */
  tags?: string[]
  /** 输入 token 定价（每百万；chat 输入与 embeddings/rerank 全量均按此计，null=不计费） */
  input_price_per_mtok?: number | string | null
  /** 输出 token 定价（每百万，仅生成类） */
  output_price_per_mtok?: number | string | null
  /** 缓存命中输入定价（每百万；null=按全价） */
  cached_input_price_per_mtok?: number | string | null
  /** 失败切换开关（默认 false） */
  failover_enabled?: boolean
  /** 切换次数上限（默认 0） */
  failover_max_retries?: number
  /** 初始端点成员列表（可空，创建即挂上 enabled=true） */
  bindings?: CreateBindingSpec[]
}

/** 调整路由定价请求体（对应后端 UpdateRouteRequest；全部可空，不传=不改） */
export interface UpdateRoutePayload {
  /** 对外模型名，不传则不改 */
  name?: string | null
  /** 模型类别，不传则不改 */
  model_type?: ModelType | null
  /** 模型描述，不传则不改（空串=清空） */
  description?: string | null
  /** 最大上下文长度（tokens），表单传最终状态（null=清空） */
  context_length?: number | null
  /** 能力标签，传 [] = 清空 */
  tags?: string[] | null
  /** 输入 token 定价（每百万；chat 输入与 embeddings/rerank 全量均按此计，可传 number 或 Decimal 字符串） */
  input_price_per_mtok?: number | string | null
  /** 输出 token 定价（每百万，仅生成类） */
  output_price_per_mtok?: number | string | null
  /** 缓存命中输入定价（每百万） */
  cached_input_price_per_mtok?: number | string | null
  /** 失败切换开关，不传则不改 */
  failover_enabled?: boolean | null
  /** 切换次数上限，不传则不改 */
  failover_max_retries?: number | null
}

/** 成员 upsert 请求体（对应后端 UpsertBindingRequest；endpoint_id 由路径参数携带，body 无需再传） */
export interface UpsertBindingPayload {
  /** 协议内权重（≥1，不传则不改） */
  weight?: number | null
  /** 优先级，不传则不改 */
  priority?: number | null
  /** 成员开关，不传则不改 */
  enabled?: boolean | null
}

/** 调整成员权重/优先级请求体（对应后端 UpdateBindingRequest；均可选，不传不改） */
export interface UpdateBindingPayload {
  /** 协议内权重（≥1），不传则不改 */
  weight?: number | null
  /** 优先级，不传则不改 */
  priority?: number | null
}

/** 列出全部发布路由（GET /api/v1/admin/publishes，无分页，每项=路由+成员列表） */
export function listPublishes(signal?: AbortSignal): Promise<PublishData[]> {
  return request({ url: '/admin/publishes', method: 'GET', signal })
}

/** 列出全部模型（模型市场视角）（GET /api/v1/admin/models-market）
 *  全部路由（published + unpublished），不按租户封禁过滤；字段与租户侧 /tenant/models 同源（RouteSchema） */
export function listMarketModels(signal?: AbortSignal): Promise<RouteData[]> {
  return request({ url: '/admin/models-market', method: 'GET', signal })
}

/** 手动创建发布路由（POST /api/v1/admin/publishes；如聚合外部端点发布新模型名；name 重复返回 409）
 *  响应复用 PublishOut（route + bindings），创建后立即可见成员列表 */
export function createRoute(data: CreateRoutePayload): Promise<PublishData> {
  return request({ url: '/admin/publishes', method: 'POST', data })
}

/** 下架路由（POST /api/v1/admin/publishes/{route_id}/unpublish） */
export function unpublishRoute(routeId: number): Promise<RouteData> {
  return request({ url: `/admin/publishes/${routeId}/unpublish`, method: 'POST' })
}

/** 重新上架路由（POST /api/v1/admin/publishes/{route_id}/republish） */
export function republishRoute(routeId: number): Promise<RouteData> {
  return request({ url: `/admin/publishes/${routeId}/republish`, method: 'POST' })
}

/** 调整路由（POST /api/v1/admin/publishes/{route_id}/update，改名/定价/failover，不传的字段不改） */
export function updateRoute(routeId: number, data: UpdateRoutePayload): Promise<RouteData> {
  return request({ url: `/admin/publishes/${routeId}/update`, method: 'POST', data })
}

/** 删除路由（POST /api/v1/admin/publishes/{route_id}/delete；连带清成员与租户授权引用；
 *  auto 来源路由会被部署 READY 钩子重建，永久下线用 unpublish） */
export function deleteRoute(routeId: number): Promise<unknown> {
  return request({ url: `/admin/publishes/${routeId}/delete`, method: 'POST' })
}

/** upsert 路由成员（POST /api/v1/admin/publishes/{route_id}/bindings/{endpoint_id}；不存在则建 weight=1，存在则改） */
export function upsertBinding(
  routeId: number,
  endpointId: number,
  data: UpsertBindingPayload,
): Promise<RouteBindingData> {
  return request({
    url: `/admin/publishes/${routeId}/bindings/${endpointId}`,
    method: 'POST',
    data,
  })
}

/** 移除路由成员（POST /api/v1/admin/publishes/{route_id}/bindings/{endpoint_id}/delete） */
export function removeBinding(routeId: number, endpointId: number): Promise<unknown> {
  return request({
    url: `/admin/publishes/${routeId}/bindings/${endpointId}/delete`,
    method: 'POST',
  })
}

/** 调整成员权重/优先级（POST /api/v1/admin/publishes/{route_id}/bindings/{endpoint_id}/update；成员不存在返回 404） */
export function updateBinding(
  routeId: number,
  endpointId: number,
  data: UpdateBindingPayload,
): Promise<RouteBindingData> {
  return request({
    url: `/admin/publishes/${routeId}/bindings/${endpointId}/update`,
    method: 'POST',
    data,
  })
}

/** 禁用成员（POST /api/v1/admin/publishes/{route_id}/bindings/{endpoint_id}/disable；临时摘除，保留记录） */
export function disableBinding(routeId: number, endpointId: number): Promise<RouteBindingData> {
  return request({
    url: `/admin/publishes/${routeId}/bindings/${endpointId}/disable`,
    method: 'POST',
  })
}

/** 启用成员（POST /api/v1/admin/publishes/{route_id}/bindings/{endpoint_id}/enable；恢复承接调用） */
export function enableBinding(routeId: number, endpointId: number): Promise<RouteBindingData> {
  return request({
    url: `/admin/publishes/${routeId}/bindings/${endpointId}/enable`,
    method: 'POST',
  })
}