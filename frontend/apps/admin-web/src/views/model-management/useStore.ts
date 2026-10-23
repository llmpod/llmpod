import type { TableRequestContext } from '@repo/hooks'
import {
  listModels,
  inspectModelSource,
  createModel,
  updateModel,
  deleteModel,
  type ModelData,
  type ModelFiles,
  type ModelSource,
  type ModelType,
  type FeasibilityReport,
  type EngineAssessment,
  type InspectModelPayload,
  type InspectResult,
} from '@/api/model'

export type {
  ModelData,
  ModelFiles,
  ModelSource,
  ModelType,
  FeasibilityReport,
  EngineAssessment,
  InspectModelPayload,
}

/** 模型列表行记录(即后端 ModelSchema) */
export type ModelRecord = ModelData

/** 添加/编辑模型表单值 */
export interface ModelFormValues {
  id?: number
  /** 模型显示名 */
  name: string
  /** 模型源 */
  source: ModelSource
  /** 来源地址(HF repo id/URL / OSS 直链) */
  source_url: string
  /** 版本/commit-hash(可选) */
  revision: string
  /** 模型类别 */
  model_type: string
  /** 权重格式(编辑时人工修正,可选) */
  format?: string | null
  /** 模型架构(可选) */
  architecture?: string
  /** 权重大小 GB(可选) */
  size_gb?: number | null
  /** 量化方案(可选) */
  quantization?: string | null
  /** 是否 MoE 架构(可选) */
  is_moe?: boolean
}

/** 模型源选项:local 支持「本机路径直填」与「浏览器分片上传」两种获取方式(向导内二级选择) */
export const SOURCE_OPTIONS: { label: string; value: ModelSource; disabled?: boolean }[] = [
  { label: 'HuggingFace', value: 'huggingface' },
  // 暂时隐藏 OSS 源(仅从选项中移除;类型/占位/文案映射保留,存量 oss 模型展示不受影响)
  // { label: 'OSS', value: 'oss' },
  { label: '本地文件', value: 'local' },
]

/** 来源地址输入占位:按模型源区分(local 对应「本机路径」模式的输入) */
export const SOURCE_URL_PLACEHOLDER: Record<ModelSource, string> = {
  huggingface: 'HF repo id 或 URL,如 Qwen/Qwen2.5-7B-Instruct',
  oss: 'OSS 文件直链,如 https://oss.example.com/models/xxx.safetensors',
  local: 'manager 本机模型目录路径,如 /data/models/Qwen2.5-7B-Instruct',
}

/** 模型类别选项 */
export const MODEL_TYPE_OPTIONS: { label: string; value: string }[] = [
  { label: 'LLM', value: 'llm' },
  { label: 'Embedding', value: 'embedding' },
  { label: 'Rerank', value: 'rerank' },
  { label: '图像', value: 'image' },
  { label: '语音识别(STT)', value: 'stt' },
  { label: '语音合成(TTS)', value: 'tts' },
  { label: '多模态', value: 'multimodal' },
]

/** 权重格式选项(探测识别,支持人工修正) */
export const FORMAT_OPTIONS: { label: string; value: string }[] = [
  { label: 'SafeTensors', value: 'safetensors' },
  { label: 'GGUF', value: 'gguf' },
  { label: 'PyTorch', value: 'pytorch' },
  { label: 'ONNX', value: 'onnx' },
  { label: '自定义', value: 'custom' },
]

/** 量化方案选项(探测识别,支持人工修正) */
export const QUANTIZATION_OPTIONS: { label: string; value: string }[] = [
  { label: '无量化', value: 'none' },
  { label: 'AWQ', value: 'awq' },
  { label: 'GPTQ', value: 'gptq' },
  { label: 'Q4_K_M', value: 'q4_k_m' },
]

/** 模型源文案映射(供表格渲染) */
export const SOURCE_LABEL: Record<string, string> = {
  huggingface: 'HuggingFace',
  oss: 'OSS',
  local: '本地',
}

/** 类别标签配色:llm 紫 / embedding 蓝 / rerank 青 走自定义色,其余默认 */
export const TYPE_TAG: Record<string, { color: string; textColor: string }> = {
  llm: { color: 'var(--c-tag-purple)', textColor: 'rgb(var(--n-primary-color))' },
  embedding: { color: 'var(--c-tag-blue)', textColor: 'var(--c-accent-blue)' },
  rerank: { color: 'var(--c-tag-teal)', textColor: 'var(--c-accent-teal)' },
}

/** 类别文案:取 MODEL_TYPE_OPTIONS 的 label,未知类别原样展示 */
export function typeLabel(modelType: string): string {
  return MODEL_TYPE_OPTIONS.find((option) => option.value === modelType)?.label ?? modelType
}

/** 引擎文案映射(供部署能力展示) */
export const ENGINE_LABEL: Record<string, string> = {
  vllm: 'vLLM',
  sglang: 'SGLang',
  llamacpp: 'llama.cpp',
}

/** ISO8601 时间格式化为 YYYY-MM-DD HH:mm;空值显示 — */
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
 * 模型管理数据 store:表格请求、探测、创建、更新均走此单例,
 * 数据来源于 /api/v1/admin/models 接口;
 * 弹窗组件内部即可完成「保存 + 提示 + 关闭」,无需父级回调。
 */
export function useStore() {
  /** 分页请求(供 useTable.request) */
  const request = async (params: { page: number; pageSize: number }, ctx?: TableRequestContext) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    const pageData = await listModels({ page: params.page, page_size: pageSize }, ctx?.signal)
    return { data: pageData.items, total: pageData.total }
  }

  /** 探测模型源(POST /inspect):可选 size_gb/model_type/model_format 覆盖探测值,回显与 feasibility 按覆盖后值重算;软失败在 files.success/message 体现 */
  const inspect = (payload: InspectModelPayload): Promise<InspectResult> => inspectModelSource(payload)

  /** 创建模型:files 为 /inspect 探测成功后的识别结果(原样回传),未探测/探测失败传 null;返回创建后的记录(quick-setup 向导据此回写) */
  const create = async (
    model: ModelFormValues,
    files: ModelFiles | null,
  ): Promise<ModelData> => {
    const created = await createModel({
      name: model.name,
      source: model.source,
      source_url: model.source_url,
      revision: model.revision || null,
      model_type: model.model_type,
      files,
    })
    notify()
    return created
  }

  /** 更新模型(PATCH 语义:基础信息 + 元数据人工修正;空串/空值按「清除该字段」传 null) */
  const update = async (model: ModelFormValues): Promise<void> => {
    await updateModel(model.id!, {
      name: model.name,
      model_type: model.model_type,
      source: model.source,
      source_url: model.source_url,
      revision: model.revision || null,
      format: model.format ?? null,
      architecture: model.architecture || null,
      size_gb: model.size_gb ?? null,
      quantization: model.quantization ?? null,
      is_moe: model.is_moe ?? false,
    })
    notify()
  }

  /** 订阅数据变更;返回取消订阅函数 */
  const subscribe = (fn: () => void): (() => void) => {
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }

  /** 删除模型:删除后 notify 联动表格刷新 */
  const remove = async (modelId: number): Promise<void> => {
    await deleteModel(modelId)
    notify()
  }

  return {
    request,
    inspect,
    create,
    update,
    remove,
    subscribe,
  }
}
