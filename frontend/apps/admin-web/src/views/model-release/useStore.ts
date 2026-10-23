import type { TableRequestContext } from '@repo/hooks'
import type { SelectOption } from 'naive-ui'
import type { EndpointData } from '@/api/endpoint'
import {
  listPublishes,
  createRoute,
  unpublishRoute,
  republishRoute,
  updateRoute,
  deleteRoute,
  upsertBinding,
  removeBinding,
  updateBinding,
  disableBinding,
  enableBinding,
  type PublishData,
  type RouteData,
  type RouteStatus,
  type ModelType,
  type RouteBindingData,
  type UpdateRoutePayload,
  type CreateRoutePayload,
  type CreateBindingSpec,
  type UpdateBindingPayload,
} from '@/api/publish'

export type { PublishData, RouteData, RouteStatus, ModelType, RouteBindingData, UpdateRoutePayload, CreateRoutePayload, CreateBindingSpec, UpdateBindingPayload }

/** 路由状态文案与标签类型映射 */
export const routeStatusMap: Record<
  RouteStatus,
  { label: string; type: 'success' | 'default' }
> = {
  published: { label: '已发布', type: 'success' },
  unpublished: { label: '已下架', type: 'default' },
}

/** 模型类别文案映射（列表展示与表单选项共用） */
export const modelTypeMap: Record<ModelType, string> = {
  llm: '语言模型',
  multimodal: '多模态',
  embedding: '向量',
  rerank: '重排',
  image: '图像生成',
  stt: '语音识别',
  tts: '语音合成',
}

/** 模型类别下拉选项（新建/编辑路由表单用） */
export const modelTypeOptions: { label: string; value: ModelType }[] = (
  Object.keys(modelTypeMap) as ModelType[]
).map((value) => ({ label: modelTypeMap[value], value }))

/** 能力标签受控词表（与后端 ModelTag 枚举对齐；新增标签=后端 ModelTag 加成员+此处补一项）
 *  纯展示用，系统不据此改任何行为；表单多选，不提供自由输入 */
export const modelTagOptions: { label: string; value: string }[] = [
  '思考',
  '工具调用',
  '视觉',
  '图像生成',
  '联网搜索',
].map((value) => ({ label: value, value }))

/** 发布路由表单模型(id 可选,新增时无) */
export interface RouteFormValues {
  id?: number
  name: string
  /** 模型类别(缺省 llm) */
  model_type: ModelType
  /** 模型描述(空串=无描述;编辑时空串=清空) */
  description: string
  /** 最大上下文长度(tokens);空=未设置 */
  context_length: number | null
  /** 能力标签(受控词表,纯展示) */
  tags: string[]
  input_price_per_mtok: string
  output_price_per_mtok: string
  cached_input_price_per_mtok: string
  failover_enabled: boolean
  failover_max_retries: number | null
  /** 创建时随请求挂上的端点(仅新建用,编辑走成员抽屉) */
  binding_endpoint_ids: number[]
}

/** 成员 upsert 表单模型 */
export interface BindingFormValues {
  endpoint_id: number | null
  weight: number | null
  priority: number | null
  enabled: boolean
}

/** Decimal 字符串去尾零展示 */
export { formatDecimal as formatPrice } from '@repo/hooks'

/** 端点列表 → 对外模型名下拉选项:取上游模型名(served_model),同名去重(按返回顺序保留首个) */
export function toModelNameOptions(list: EndpointData[]): SelectOption[] {
  const seen = new Set<string>()
  const options: SelectOption[] = []
  for (const item of list) {
    if (seen.has(item.served_model)) continue
    seen.add(item.served_model)
    options.push({ label: item.served_model, value: item.served_model })
  }
  return options
}

/** 端点列表 → 挂载供应商下拉选项(名称+供应商区分同名端点;供应商空=外部端点) */
export function toProviderOptions(list: EndpointData[]): SelectOption[] {
  return list.map((item) => ({
    label: `${item.name}（${item.provider_name ?? '外部'}）`,
    value: item.id,
  }))
}

/** 选中模型名 → 默认全选支持该模型的全部端点 id(端点 served_model 与模型名一致即视为支持) */
export function defaultBindingEndpointIds(list: EndpointData[], modelName: string): number[] {
  if (!modelName) return []
  return list.filter((item) => item.served_model === modelName).map((item) => item.id)
}

/**
 * 折叠区(模型描述/标签/定价/failover)是否有已设值:
 * 编辑回填有任一值时表单默认展开,避免已设值不可见;新建/全空值时默认收起。
 */
export function hasAdvancedSettings(initial?: RouteData | null): boolean {
  if (!initial) return false
  return (
    !!initial.description ||
    initial.tags.length > 0 ||
    !!initial.input_price_per_mtok ||
    !!initial.output_price_per_mtok ||
    !!initial.cached_input_price_per_mtok ||
    initial.failover_enabled ||
    initial.failover_max_retries > 0
  )
}

/** 变更监听器 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/**
 * 模型发布数据 store:列表请求、上下架、定价编辑、成员绑定管理均走此单例,
 * 数据来源于 /api/v1/admin/publishes 接口(全量返回,keyword 前端过滤,remote 分页)。
 */
export function useStore() {
  /** 全量请求(供 useTable.request,keyword 前端过滤 route.name 后按页切片,remote 分页) */
  const request = async (
    params: { page?: number; pageSize?: number; keyword?: string },
    ctx?: TableRequestContext,
  ) => {
    let list = await listPublishes(ctx?.signal)
    const keyword = (params.keyword ?? '').trim().toLowerCase()
    if (keyword) {
      list = list.filter((p) => p.route.name.toLowerCase().includes(keyword))
    }
    // 接口无服务端分页,此处前端切片,否则 remote 模式整页数据一次性渲染
    const page = params.page ?? 1
    const pageSize = params.pageSize ?? 10
    const start = (page - 1) * pageSize
    return { data: list.slice(start, start + pageSize), total: list.length }
  }

  /** 下架路由 */
  const unpublish = async (routeId: number): Promise<void> => {
    await unpublishRoute(routeId)
    notify()
  }

  /** 重新上架 */
  const republish = async (routeId: number): Promise<void> => {
    await republishRoute(routeId)
    notify()
  }

  /** 手动创建发布路由(即建即上架,可随请求挂初始成员;name 重复 409 由 HTTP 层提示,表单保留) */
  const create = async (model: RouteFormValues): Promise<{ name: string; bindingCount: number }> => {
    const bindings: CreateBindingSpec[] = model.binding_endpoint_ids.map((endpoint_id) => ({
      endpoint_id,
    }))
    const result = await createRoute({
      name: model.name,
      model_type: model.model_type,
      // 创建时空描述传 null(后端 description 可空)
      description: model.description || null,
      context_length: model.context_length ?? null,
      tags: model.tags,
      input_price_per_mtok: model.input_price_per_mtok || null,
      output_price_per_mtok: model.output_price_per_mtok || null,
      cached_input_price_per_mtok: model.cached_input_price_per_mtok || null,
      failover_enabled: model.failover_enabled,
      failover_max_retries: model.failover_max_retries ?? 0,
      ...(bindings.length ? { bindings } : {}),
    })
    notify()
    return { name: result.route.name, bindingCount: result.bindings.length }
  }

  /** 编辑路由(改名/定价/failover) */
  const update = async (model: RouteFormValues): Promise<{ name: string }> => {
    if (!model.id) throw new Error('缺少路由 ID')
    await updateRoute(model.id, {
      name: model.name,
      model_type: model.model_type,
      // 编辑时空串=清空描述(表单即最终状态)
      description: model.description,
      context_length: model.context_length ?? null,
      // 传最终状态:[]=清空(表单即最终状态)
      tags: model.tags,
      input_price_per_mtok: model.input_price_per_mtok || null,
      output_price_per_mtok: model.output_price_per_mtok || null,
      cached_input_price_per_mtok: model.cached_input_price_per_mtok || null,
      failover_enabled: model.failover_enabled,
      failover_max_retries: model.failover_max_retries,
    })
    notify()
    return { name: model.name }
  }

  /** 删除路由(连带清成员与租户授权引用;部署自动发布的路由会被 READY 钩子重建,永久下线用 unpublish) */
  const remove = async (routeId: number): Promise<void> => {
    await deleteRoute(routeId)
    notify()
  }

  /** upsert 成员绑定;返回绑定结果供弹窗本地更新列表 */
  const upsertBindingItem = async (
    routeId: number,
    data: BindingFormValues,
  ): Promise<RouteBindingData> => {
    if (!data.endpoint_id) throw new Error('请选择端点')
    const binding = await upsertBinding(routeId, data.endpoint_id, {
      weight: data.weight,
      priority: data.priority,
      enabled: data.enabled,
    })
    notify()
    return binding
  }

  /** 调整成员权重/优先级;返回绑定结果供抽屉本地更新行 */
  const updateBindingItem = async (
    routeId: number,
    endpointId: number,
    data: UpdateBindingPayload,
  ): Promise<RouteBindingData> => {
    const binding = await updateBinding(routeId, endpointId, data)
    notify()
    return binding
  }

  /** 切换成员启用状态(禁用=临时摘除,启用=恢复);返回最新绑定供抽屉本地更新行 */
  const toggleBindingItem = async (
    routeId: number,
    binding: RouteBindingData,
  ): Promise<RouteBindingData> => {
    const updated = binding.enabled
      ? await disableBinding(routeId, binding.endpoint_id)
      : await enableBinding(routeId, binding.endpoint_id)
    notify()
    return updated
  }

  /** 移除成员绑定 */
  const removeBindingItem = async (routeId: number, endpointId: number): Promise<void> => {
    await removeBinding(routeId, endpointId)
    notify()
  }

  /** 订阅数据变更 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return {
    request,
    create,
    unpublish,
    republish,
    update,
    remove,
    upsertBindingItem,
    updateBindingItem,
    toggleBindingItem,
    removeBindingItem,
    subscribe,
  }
}
