import { request } from './request'

/** KV cache 精度选项(后端 adapter 声明的支持矩阵;前端 Step2 高级参数下拉直接渲染) */
export interface KVCacheDtypeOption {
  /** 选项值;空串 = 默认不量化(不写 flag) */
  value: string
  /** 展示名 */
  label: string
  /** KV 元素字节数(f16/bf16=2、fp8/q8_0=1、q4_0=0.5;估算单一事实源) */
  dtype_bytes: number
  /** KV 占用倍率(相对 f16,由 dtype_bytes 派生;前端标注用) */
  kv_ratio: number
  /** 选中后写入 engine_params.extra_args 的原生 flag 序列(依赖 flag 一并给出) */
  args: string[]
  /** 选项说明(精度/兼容性提示) */
  hint: string
}

/** 引擎能力字典项(GET /api/v1/admin/inference-images/engines;「引擎支持哪些架构/格式/类别」的唯一完整来源) */
export interface EngineOut {
  /** 引擎唯一标识:vllm | sglang | llamacpp */
  engine: string
  /** 引擎展示名(下拉/回显直接用,无需前端再映射) */
  display_name: string
  /** 支持的权重格式:safetensors | gguf */
  supported_formats: string[]
  /** 支持的软件栈:nvidia | cann | cpu(架构过滤的事实源) */
  supported_gpu_archs: string[]
  /** 支持的模型类别:llm | embedding | rerank | multimodal | image | stt | tts */
  supported_categories: string[]
  /** 支持的并行方式:tp | pp | ep */
  supported_parallel: string[]
  /** 参数生成是否已实现;false 为占位引擎,不可选 */
  deployable: boolean
  /** 系统必控 flag(用户 extra_args 同名会被忽略) */
  protected_flags: string[]
  /** 推荐参数 chip:{name/hint/default/choices}(后端为自由对象,Step3 接入时再细化) */
  param_suggestions: Record<string, unknown>[]
  /** KV cache 精度选项(Step2 高级参数下拉数据源;空表=引擎无此能力,前端不渲染该控件) */
  kv_cache_dtype_options: KVCacheDtypeOption[]
  /** 按 model_id 判定:true=类别+格式都支持;false=不支持(前端置灰);null=未传 model_id/无法判定 */
  supports_model?: boolean | null
  /** supports_model=false 时的人读原因(置灰项标签后缀) */
  unsupported_reason?: string | null
  /** 引擎是否支持多机部署(引擎级,不感知镜像版本);缺失 → 前端宽松兜底 */
  supports_multinode?: boolean | null
}

/**
 * 引擎能力字典:进页面预取一次并缓存,引擎下拉过滤/架构灰化/Step3 参数预设均从它派生。
 * 传 modelId 时后端额外按该模型判定支持性(supports_model),供 Step1 置灰不支持的引擎。
 */
export function listInferenceEngines(modelId?: number): Promise<EngineOut[]> {
  return request({
    url: '/admin/inference-images/engines',
    method: 'GET',
    params: modelId === undefined ? undefined : { model_id: modelId },
  })
}

/** 镜像目录条目(GET /api/v1/admin/inference-images;同 engine_version 可多条 gpu_model/driver 变体) */
export interface ImageEntryOut {
  /** 变体唯一标识 */
  id: string
  engine: string
  arch: string
  /** 适配的 GPU 型号 glob(fnmatch Gpu.name) */
  gpu_model: string[]
  /** 驱动版本区间,如 >=570 / >=530,<570 */
  driver: string | null
  /** 引擎版本(= 部署 image_tag 维度,版本下拉分组键) */
  engine_version: string | null
  /** 完整镜像 ref */
  image: string
  /** 是否 catalog 默认(无精确匹配时预选该版本) */
  default: boolean
  /** 版本说明,下拉项副标题 */
  notes: string
}

/** 镜像目录查询条件(OAS 另有 engine_version/gpu_model/driver_version 维度,当前前端只用 engine/arch) */
export interface ListInferenceImagesParams {
  engine?: string
  arch?: string
}

/**
 * 镜像目录:Step1 按引擎渲染镜像版本下拉。
 * 选中版本提交时传 spec.image_tag(变体裁决留给后端 catalog),不传完整 image。
 */
export function listInferenceImages(
  params: ListInferenceImagesParams = {},
): Promise<ImageEntryOut[]> {
  return request({ url: '/admin/inference-images', method: 'GET', params })
}
