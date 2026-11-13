import { request } from './request'

/**
 * 模型来源:
 * huggingface | modelscope | oss | local(local 为分片上传后的 staged 路径) | node(节点盘上就地引用)
 */
export type ModelSource = 'huggingface' | 'modelscope' | 'oss' | 'local' | 'node'

/** 模型类别:llm|embedding|rerank|image|stt|tts|multimodal(后端为字符串非枚举) */
export type ModelType = 'llm' | 'embedding' | 'rerank' | 'image' | 'stt' | 'tts' | 'multimodal'

/** 模型信息(对应后端 ModelSchema) */
export interface ModelData {
  /** 主键ID(自增) */
  id: number
  /** 模型名称 */
  name: string
  /** 模型类别:llm | embedding | rerank | image | stt | tts */
  model_type: string
  /** 模型架构,如 qwen2 / llama3(可空) */
  architecture: string | null
  /** 是否 MoE 架构 */
  is_moe: boolean
  /** 是否视觉模型 */
  is_vision: boolean
  /** 权重大小 GB(可空) */
  size_gb: number | null
  /** 格式:safetensors | gguf | ...(可空) */
  format: string | null
  /** 量化方案:awq | gptq | none(可空) */
  quantization: string | null
  /** 权重哈希(可空) */
  hash: string | null
  /** 已下载该模型的节点数(GET /models 列表下发;列表「已下载节点」列展示) */
  node_count?: number | null
  /** 来源:huggingface | modelscope | oss | local | node(可空) */
  source: string | null
  /** 来源地址(可空) */
  source_url: string | null
  /** 来源盘上布局:unknown | single_gguf | gguf_shards | transformers_folder | diffusers_folder */
  source_kind?: string | null
  /** 多 variant 仓选哪个变体的子路径(可空) */
  source_subpath?: string | null
  /** 就地引用模型绑定的节点 ID(source=node 时有值,该模型只能部署到这台节点) */
  node_id?: string | null
  /** 来源版本标识/commit-hash(可空) */
  revision: string | null
  created_at: string
  updated_at: string
}

/** 模型分页数据(GET /api/v1/admin/models) */
export interface ModelPageData {
  items: ModelData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 模型源识别结果(对应后端 ModelFiles):/inspect 返回回显,create 时原样回传 */
export interface ModelFiles {
  /** 探测是否成功(False 时前端可提示后人工填) */
  success: boolean
  /** 失败原因 / 警告,直接展示 */
  message: string | null
  /** 结构形态:single_file | directory */
  structure: string | null
  /** 权重格式:safetensors|gguf|pytorch|onnx|custom */
  format: string | null
  /** 布局:transformers|diffusers|gguf|onnx|... */
  layout: string | null
  /** 是否有可读的 config */
  has_config: boolean
  /** 权重文件名清单(回显参考) */
  weight_files: string[] | null
  /** 身份标识原样字符串(config.architectures[0] 等) */
  architecture: string | null
  /** diffusers model_index._class_name */
  pipeline_class: string | null
  /** HF 元数据 task 标签(仅 HF 源) */
  pipeline_tag: string | null
  /** 是否 MoE */
  is_moe: boolean
  /** 量化方案:awq|gptq|q4_k_m|none|... */
  quantization: string | null
  /** 权重大小 GB */
  size_gb: number | null
  /** 来源版本/commit-hash */
  revision: string | null
  /** 建议 model_type(None=用户选) */
  suggested_type: string | null
}

/** 单引擎对模型的支持判定(对应后端 EngineAssessment) */
export interface EngineAssessment {
  /** 引擎名:vllm | sglang | llamacpp */
  engine: string
  /** True=类别+格式都支持;False=明确不支持;None=type/格式未定无法判定 */
  supported: boolean | null
  /** 不支持 / 未定的原因 */
  reasons: string[]
}

/** 可部署性检测报告(对应后端 FeasibilityReport,仅声明前端展示所需字段) */
export interface FeasibilityReport {
  /** 整体是否可部署 */
  deployable: boolean
  /** 人读总结 */
  message: string | null
  /** 各引擎对模型的支持判定 */
  engines: EngineAssessment[]
}

/** /inspect 响应:文件识别 + 可部署性检测(best-effort,失败为 null) */
export interface InspectResult {
  files: ModelFiles
  feasibility: FeasibilityReport | null
}

/** 列出模型查询参数 */
export interface ListModelsParams {
  /** 按类别过滤:llm|embedding|rerank|image|stt|tts|multimodal */
  model_type?: string
  /** 页码,从1开始 */
  page?: number
  /** 每页条数 */
  page_size?: number
}

/** 列出模型(GET /api/v1/admin/models) */
export function listModels(params: ListModelsParams, signal?: AbortSignal): Promise<ModelPageData> {
  return request({ url: '/admin/models', method: 'GET', params, signal })
}

/** 上传前预检的文件清单项(名+大小,文件还未落盘) */
export interface ManifestFileSpec {
  /** 文件名(含扩展名) */
  name: string
  /** 文件大小(字节) */
  size: number
}

/** 预检警告级别:fatal=阻止上传;warn=需用户确认后继续;info=仅提示 */
export type ManifestWarningSeverity = 'fatal' | 'warn' | 'info'

/** 单条预检警告 */
export interface ManifestWarning {
  /** 规则标识,如 no_weight_file */
  code: string
  /** 人读原因,直接展示 */
  message: string
  severity: ManifestWarningSeverity
  /** 关联文件名(与具体文件无关时为 null) */
  file: string | null
}

/** validate-manifest 响应 */
export interface ValidateManifestResult {
  warnings: ManifestWarning[]
}

/** 上传前预检(POST /api/v1/admin/models/validate-manifest):吃纯文件清单,复用 inference.py 权重/布局规则 */
export function validateManifest(data: {
  files: ManifestFileSpec[]
}): Promise<ValidateManifestResult> {
  return request({ url: '/admin/models/validate-manifest', method: 'POST', data })
}

/** 探测模型源请求体(POST /api/v1/admin/models/inspect);size_gb/model_type/model_format 为可选覆盖:不传用探测值,传了以用户值为准且回显/feasibility 按覆盖后值重算 */
export interface InspectModelPayload {
  /** 来源:huggingface | modelscope | oss | local | node */
  source: string
  /** 来源地址:HF/MS repo id/URL / OSS 文件直链 / manager 本地路径 / 节点上的绝对目录 */
  source_url: string
  /** 版本/commit-hash(HF 默认 main,ModelScope 默认 master;不填由后端钉 commit) */
  revision?: string | null
  /** 覆盖:权重大小 GB(探测不到时 feasibility.deployable 恒为 false,手填后才解锁判定) */
  size_gb?: number | null
  /** 覆盖:模型类别(回显 suggested_type 即该值) */
  model_type?: string | null
  /** 覆盖:权重格式 safetensors|gguf|pytorch|onnx|... */
  model_format?: string | null
  /** 节点 ID(source=node 必填:探测读取该节点磁盘上的目录;缺省 422) */
  node_id?: string | null
}

/** 探测模型源(POST /api/v1/admin/models/inspect,抓文件清单/config,不下载权重) */
export function inspectModelSource(data: InspectModelPayload): Promise<InspectResult> {
  return request({ url: '/admin/models/inspect', method: 'POST', data })
}

/** 创建模型请求体(POST /api/v1/admin/models;files 取自 /inspect 识别结果,为空则仅落基础信息) */
export interface CreateModelPayload {
  /** 模型显示名 */
  name: string
  /** 来源:huggingface | modelscope | oss | local | node */
  source: string
  /** 来源地址(HF/MS repo id/URL / OSS 直链 / manager 本地路径 / 节点上的绝对目录) */
  source_url: string
  /** 版本/commit-hash */
  revision?: string | null
  /** 模型类别(用户选;前端可预填 /inspect 的 suggested_type) */
  model_type?: string
  /** /inspect 识别结果(前端回显后回传) */
  files?: ModelFiles | null
  /** 来源凭证(HuggingFace Token / ModelScope Access Token);加密保存且不在响应中返回 */
  source_auth?: string | null
  /** 节点 ID(source=node 必填:模型就地绑定该节点,权重不复制) */
  node_id?: string | null
}

/** 创建模型(POST /api/v1/admin/models) */
export function createModel(data: CreateModelPayload): Promise<ModelData> {
  return request({ url: '/admin/models', method: 'POST', data })
}

/** 更新模型请求体:全可选 PATCH 语义,仅传字段生效 */
export interface UpdateModelPayload {
  name?: string
  model_type?: string
  architecture?: string | null
  is_moe?: boolean
  is_vision?: boolean
  size_gb?: number | null
  format?: string | null
  quantization?: string | null
  source?: string
  source_url?: string
  revision?: string | null
  /** 多 variant 仓变体子路径(显式 null 清空) */
  source_subpath?: string | null
  /**
   * 来源凭证三态:不传=保留原 token;null=清除;字符串=设置新 token(加密入库)。
   * 前端「令牌」输入为空时不下发该字段,即保留原值。
   */
  source_auth?: string | null
  /** 层数 */
  num_layers?: number | null
  /** KV head 数(GQA 下 ≠ attention heads) */
  num_kv_heads?: number | null
  /** 每 head 维度 */
  head_dim?: number | null
  /** 模型自带上下文上限 */
  max_context?: number | null
  /** GGUF 多模态视觉投影器文件(repo 相对路径) */
  mmproj_file?: string | null
  /** 模型带自定义代码,部署需 --trust-remote-code */
  needs_trust_remote_code?: boolean
}

/** 更新模型(POST /api/v1/admin/models/{model_id}/update) */
export function updateModel(modelId: number, data: UpdateModelPayload): Promise<ModelData> {
  return request({ url: `/admin/models/${modelId}/update`, method: 'POST', data })
}

/** 删除模型(POST /api/v1/admin/models/{model_id}/delete) */
export function deleteModel(modelId: number): Promise<void> {
  return request({ url: `/admin/models/${modelId}/delete`, method: 'POST' })
}

/** 模型在某节点上的副本(对应后端模型节点副本项;字段缺失时前端按 -- 展示) */
export interface ModelNodeCopy {
  /** 节点 ID */
  node_id: string
  /** 节点主机名 */
  hostname?: string | null
  /** 节点 IP */
  ip?: string | null
  /** 该节点上该模型的本地目录(后端字段名 local_dir) */
  local_dir?: string | null
  /** 节点状态:online | offline | maintenance | busy | drained(与模型状态分开展示) */
  node_status?: string | null
  /** 模型在该节点上的状态:present | absent | downloading | verifying | failed(与节点状态分开展示) */
  model_status?: string | null
  /** 该节点上的权重总字节数(后端字段名 bytes_total;展示时换算成 GB) */
  bytes_total?: number | null
  /** 最近一次在位校验时间(ISO8601;后端字段名 last_checked_at) */
  last_checked_at?: string | null
}

/** 模型已下载节点副本列表(GET /api/v1/admin/models/{model_id}/nodes;后端返回数组) */
export function listModelNodes(
  modelId: number,
  signal?: AbortSignal,
): Promise<ModelNodeCopy[]> {
  return request({ url: `/admin/models/${modelId}/nodes`, method: 'GET', signal })
}

/**
 * 删除模型在某节点上的副本(POST /api/v1/admin/models/{model_id}/nodes/{node_id}/delete)。
 * delete_files=false(默认)仅移除平台记录、磁盘文件保留;true 删除节点上的模型目录并移除记录。
 * 该节点仍有部署(含规划中)时 409;节点离线或删除失败 400(记录保留)。
 */
export function deleteModelNode(
  modelId: number,
  nodeId: string,
  deleteFiles = false,
): Promise<void> {
  return request({
    url: `/admin/models/${modelId}/nodes/${nodeId}/delete`,
    method: 'POST',
    data: { delete_files: deleteFiles },
  })
}
