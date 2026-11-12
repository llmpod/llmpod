import type { TableRequestContext } from '@repo/hooks'
import {
  listEndpoints,
  createEndpoints,
  fetchUpstreamModels as fetchUpstreamModelsApi,
  updateEndpoint,
  deleteEndpoint,
  type EndpointData,
  type EndpointProtocol,
  type EndpointHealthStatus,
  type CreateEndpointItem,
  type CreateEndpointsPayload,
  type FetchUpstreamModelsPayload,
  type UpdateEndpointPayload,
} from '@/api/endpoint'

export type {
  EndpointData,
  EndpointProtocol,
  EndpointHealthStatus,
  CreateEndpointItem,
  CreateEndpointsPayload,
  FetchUpstreamModelsPayload,
  UpdateEndpointPayload,
}

/** 协议选项 */
export const protocolOptions = [
  { label: 'OpenAI', value: 'openai' },
  { label: 'Anthropic', value: 'anthropic' },
]

/** 健康状态文案与标签类型映射 */
export const healthStatusMap: Record<
  EndpointHealthStatus,
  { label: string; type: 'success' | 'warning' | 'error' }
> = {
  healthy: { label: '健康', type: 'success' },
  degraded: { label: '降级', type: 'warning' },
  down: { label: '异常', type: 'error' },
}

/** 健康状态选项（表单用） */
export const healthStatusFormOptions = [
  { label: '健康', value: 'healthy' },
  { label: '降级', value: 'degraded' },
  { label: '异常', value: 'down' },
]

/** 表单模型 */
export interface EndpointFormValues {
  id?: number
  protocol: EndpointProtocol
  api_base: string
  /** 新建：上游模型名多选（提交时归并进 models 数组） */
  served_models: string[]
  /** 编辑：单条上游模型名（/{id}/update 契约） */
  served_model: string
  name: string
  api_key: string
  provider_name: string
  path: string
  operations: string[]
}

/**
 * 上游模型名归一化：手动输入的标签逐个保留，仅去首尾空白并去重
 * （同一批里 served_model 重复会被后端判 422，一票否决整批）。
 */
export function normalizeModelIds(values: readonly string[] | null | undefined): string[] {
  const result: string[] = []
  for (const value of values ?? []) {
    const name = String(value).trim()
    if (name && !result.includes(name)) result.push(name)
  }
  return result
}

/** 变更监听器 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/**
 * 来源端点数据 store:表格请求、增删改均走此单例,
 * 数据来源于 /api/v1/admin/endpoints 接口（全量返回，前端 local 模式分页）。
 */
export function useStore() {
  /** 全量请求(供 useTable.request，local 模式前端分页+筛选) */
  const request = async (_params: unknown, ctx?: TableRequestContext) => {
    const list = await listEndpoints(ctx?.signal)
    return { data: list, total: list.length }
  }

  /**
   * 新增端点（POST /admin/endpoints 收模型数组，单条即长度 1）。
   * name 一律不下发：后端缺省取 served_model，而空串会因 min_length=1 让整批 422。
   */
  const create = async (model: EndpointFormValues): Promise<EndpointData[]> => {
    const models: CreateEndpointItem[] = normalizeModelIds(model.served_models).map(
      (servedModel) => ({ served_model: servedModel }),
    )
    const created = await createEndpoints({
      protocol: model.protocol,
      api_base: model.api_base,
      api_key: model.api_key || null,
      provider_name: model.provider_name || null,
      path: model.path || null,
      models,
    })
    notify()
    return created
  }

  /** 拉取上游模型清单（登记前发现，不落库）；失败静默返回，调用方在下拉空态里引导手动输入 */
  const fetchUpstreamModels = (payload: FetchUpstreamModelsPayload): Promise<string[]> =>
    fetchUpstreamModelsApi(payload)

  /** 调整端点 */
  const update = async (model: EndpointFormValues): Promise<{ name: string }> => {
    if (!model.id) throw new Error('缺少端点 ID')
    const payload: UpdateEndpointPayload = {
      api_base: model.api_base || null,
      served_model: model.served_model || null,
      name: model.name || null,
      provider_name: model.provider_name || null,
      path: model.path || null,
    }
    // 「不填=不修改」契约:api_key 留空时不下发该键——显式 null 会被后端视为改动(可能清空已存 Key)
    if (model.api_key.trim() !== '') payload.api_key = model.api_key
    await updateEndpoint(model.id, payload)
    notify()
    return { name: model.name }
  }

  /** 切换启用状态 */
  const toggleEnabled = async (row: EndpointData): Promise<boolean> => {
    await updateEndpoint(row.id, { enabled: !row.enabled })
    notify()
    return !row.enabled
  }

  /** 删除端点 */
  const remove = async (id: number): Promise<void> => {
    await deleteEndpoint(id)
    notify()
  }

  /** 订阅数据变更 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { request, create, update, toggleEnabled, remove, fetchUpstreamModels, subscribe }
}