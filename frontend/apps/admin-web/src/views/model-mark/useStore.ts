import { ref } from 'vue'
import type { Component } from 'vue'
import { CubeOutline } from '@vicons/ionicons5'
import { listMarketModels, type ModelType, type RouteData, type RouteStatus } from '@/api/publish'
import { getProxyEndpoint } from '@/api/proxy-endpoint'
import { modelBrandIcon } from '@/views/node-pool/topology/components/brandIcons'

export type { RouteData }

/** 模型类别文案映射(卡片 tag 与详情抽屉共用) */
export const modelTypeMap: Record<ModelType, string> = {
  llm: '语言模型',
  multimodal: '多模态',
  embedding: '向量',
  rerank: '重排',
  image: '图像生成',
  stt: '语音识别',
  tts: '语音合成',
}

/** 路由状态文案与标签类型映射(管理员市场含已下架条目) */
export const routeStatusMap: Record<
  RouteStatus,
  { label: string; type: 'success' | 'default' }
> = {
  published: { label: '已发布', type: 'success' },
  unpublished: { label: '已下架', type: 'default' },
}

/** 创建来源文案 */
export const routeOriginMap: Record<RouteData['origin'], string> = {
  auto: '部署自动发布',
  manual: '手动创建',
}

/** Decimal 字符串去尾零展示("2.000000" → "2","0.0000005" → "0.0000005" 不进科学计数法);空值显示 — */
export { formatDecimal } from '@repo/hooks'

/** 上下文长度展示:千分位 + tokens;空值显示 — */
export function formatContextLength(value: number | null): string {
  if (value == null) return '—'
  return `${new Intl.NumberFormat('en-US').format(value)} tokens`
}

/** 模型品牌图标:按名称识别品牌家族(Qwen/DeepSeek/Gemma/KIMI/MiniMax/GLM 等),未命中回退通用立方体图标 */
export function brandIconOf(name: string): Component {
  return modelBrandIcon(name) ?? CubeOutline
}

/** 变更监听器:重新拉取后触发,供页面联动刷新 */
const listeners = new Set<() => void>()
function notify(): void {
  listeners.forEach((fn) => fn())
}

/** 全部模型全集(含已下架;不按租户封禁过滤,后端保证) */
const records = ref<RouteData[]>([])
/** 加载态:请求期间为 true */
const loading = ref(false)

/** 数据面接入基址(来自 /api/v1/proxy-endpoint;未加载或失败为 null,详情抽屉展示占位) */
const proxyBaseUrl = ref<string | null>(null)
/** 接入地址是否已成功加载(成功后不再重复请求) */
let proxyEndpointLoaded = false

/**
 * 模型市场数据 store:数据来源于 /api/v1/admin/models-market(全量,无分页)。
 */
export function useStore() {
  /** 拉取全部模型(管理员视角=全集) */
  async function fetchModels(signal?: AbortSignal): Promise<void> {
    loading.value = true
    try {
      records.value = await listMarketModels(signal)
      notify()
    } finally {
      loading.value = false
    }
  }

  /** 拉取数据面接入基址(失败静默,不阻塞模型列表;下次进入页面可重试) */
  async function fetchProxyEndpoint(signal?: AbortSignal): Promise<void> {
    if (proxyEndpointLoaded) return
    try {
      const endpoint = await getProxyEndpoint(signal)
      // 去尾斜杠,拼接路径时不再处理
      proxyBaseUrl.value = endpoint.base_url.replace(/\/+$/, '')
      proxyEndpointLoaded = true
    } catch {
      // 加载失败保持 null,详情抽屉展示占位
    }
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  return { records, loading, fetchModels, proxyBaseUrl, fetchProxyEndpoint, subscribe }
}
