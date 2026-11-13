import type { TableRequestContext } from '@repo/hooks'
import {
  listEndpoints,
  createEndpoints,
  fetchUpstreamModels as fetchUpstreamModelsApi,
  testEndpoint as testEndpointApi,
  updateEndpoint,
  deleteEndpoint,
  type EndpointData,
  type EndpointProtocol,
  type EndpointHealthStatus,
  type CreateEndpointItem,
  type CreateEndpointsPayload,
  type FetchUpstreamModelsPayload,
  type TestEndpointPayload,
  type EndpointProbeResult,
  type UpdateEndpointPayload,
} from '@/api/endpoint'

export type {
  EndpointData,
  EndpointProtocol,
  EndpointHealthStatus,
  CreateEndpointItem,
  CreateEndpointsPayload,
  FetchUpstreamModelsPayload,
  TestEndpointPayload,
  EndpointProbeResult,
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

/** 待登记的一行模型：上游模型名 + 可选展示名 */
export interface EndpointModelItem {
  /** 上游模型名（外部=官方名，如 gpt-4o） */
  served_model: string
  /** 端点展示名；留空=后端取 served_model */
  name: string
}

/** 造一行空模型（每次新对象，避免多行共享同一引用） */
export function createModelItem(servedModel = '', name = ''): EndpointModelItem {
  return { served_model: servedModel, name }
}

/** 表单模型 */
export interface EndpointFormValues {
  id?: number
  protocol: EndpointProtocol
  api_base: string
  /** 新建：多行模型（每行 上游模型名 + 显示名） */
  models: EndpointModelItem[]
  /** 编辑：单条上游模型名（/{id}/update 契约） */
  served_model: string
  name: string
  api_key: string
  provider_name: string
  path: string
  operations: string[]
}

/**
 * 行数据归一化成可提交的模型项：去首尾空白、丢弃模型 ID 为空的行、
 * 按模型名去重保序（同一批里 served_model 重复会被后端判 422，一票否决整批）。
 * 显示名为空时不下发 name 键——空串会因 min_length=1 让整批 422，缺省值交给后端。
 */
export function normalizeModels(
  items: readonly EndpointModelItem[] | null | undefined,
): CreateEndpointItem[] {
  const result: CreateEndpointItem[] = []
  const seen = new Set<string>()
  for (const item of items ?? []) {
    const servedModel = String(item?.served_model ?? '').trim()
    if (!servedModel || seen.has(servedModel)) continue
    seen.add(servedModel)
    const name = String(item?.name ?? '').trim()
    result.push(name ? { served_model: servedModel, name } : { served_model: servedModel })
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
   * 每行均可带显示名；留空时不下发 name（后端缺省取 served_model）。
   */
  const create = async (model: EndpointFormValues): Promise<EndpointData[]> => {
    const created = await createEndpoints({
      protocol: model.protocol,
      api_base: model.api_base,
      api_key: model.api_key || null,
      provider_name: model.provider_name || null,
      path: model.path || null,
      models: normalizeModels(model.models),
    })
    notify()
    return created
  }

  /** 拉取上游模型清单（登记前发现，不落库）；失败静默返回，调用方在候选空态里引导手动输入 */
  const fetchUpstreamModels = (payload: FetchUpstreamModelsPayload): Promise<string[]> =>
    fetchUpstreamModelsApi(payload)

  /**
   * 探测端点连通性（真发一次最小 chat 请求，结论恒走 200）。
   * 探的是该行已存配置，后端会把结论写回该行健康 —— 探完 notify 让列表跟着更新，
   * 否则「Key 被上游吊销」这类问题会一直显示健康。
   */
  const testConnection = async (id: number): Promise<EndpointProbeResult> => {
    const result = await testEndpointApi({ endpoint_id: id })
    notify()
    return result
  }

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

  return {
    request,
    create,
    update,
    toggleEnabled,
    remove,
    fetchUpstreamModels,
    testConnection,
    subscribe,
  }
}