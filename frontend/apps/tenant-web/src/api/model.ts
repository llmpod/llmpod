import { request } from './request'

/** 路由发布状态：published=已发布 | unpublished=已下架 */
export type RouteStatus = 'published' | 'unpublished'

/** 路由创建来源：auto=部署自动发布 | manual=管理面手工建 */
export type RouteOrigin = 'auto' | 'manual'

/** 模型类别（对应后端 model_type） */
export type ModelType = 'llm' | 'multimodal' | 'embedding' | 'rerank' | 'image' | 'stt' | 'tts'

/** 可用模型路由（对应后端 RouteSchema；= 已发布(published) − 被封禁(blocked)，无需前端再过滤） */
export interface RouteData {
  id: number
  /** 对外模型名（全局唯一，调用时的 model 字段） */
  name: string
  /** 发布状态（本端点恒为 published） */
  status: RouteStatus
  /** 创建来源：auto=部署自动发布 | manual=管理面手工建 */
  origin: RouteOrigin
  /** 模型类别 */
  model_type: ModelType
  /** 模型描述（可空） */
  description: string | null
  /** 最大上下文长度（tokens；可空） */
  context_length: number | null
  /** 能力标签列表（受控词表：思考/工具调用/视觉/图像生成/联网搜索；纯展示，系统不处理） */
  tags: string[]
  /** 输入 token 定价（每百万 token；chat 输入与 embeddings/rerank 全量均按此计，null=不计费，Decimal 字符串） */
  input_price_per_mtok: string | null
  /** 输出 token 定价（每百万 token，仅生成类，null=不计费，Decimal 字符串） */
  output_price_per_mtok: string | null
  /** 缓存命中输入定价（每百万，null=未配置按全价计，Decimal 字符串） */
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

/** 列出本租户可调用模型全集（GET /api/v1/tenant/models，list[RouteSchema]，未发布/被封禁的路由不出现） */
export function listTenantModels(signal?: AbortSignal): Promise<RouteData[]> {
  return request({ url: '/tenant/models', method: 'GET', signal })
}
