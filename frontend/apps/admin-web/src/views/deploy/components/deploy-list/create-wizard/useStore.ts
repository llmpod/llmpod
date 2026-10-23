// @unocss-include
/**
 * 新建部署向导:会话状态(模块级单例)。
 * deploy-list/useStore 管列表数据与写操作;本 store 管向导的步骤/表单值/集群选项/拓扑/预检/选卡。
 * 顶部 @unocss-include:paramSuggestionOptions 的下拉项 render 内联了 UnoCSS 工具类,
 * 纯 .ts 不在 unocss 默认扫描范围(vue|svelte|[jt]sx|md|html),需该注释显式纳入提取。
 */
import { computed, h, reactive, ref, shallowRef, type VNode, type VNodeChild } from 'vue'
import { listClusters, type ClusterData } from '@/api/cluster'
import {
  inspectDeployment,
  type CreateDeploymentPayload,
  type EngineParams,
  type InspectClusterGpu,
  type InspectDeploymentResult,
  type ModelDeploymentOut,
  type NodeSupportInfo,
  type PublishConfig,
} from '@/api/deployment'
import {
  listInferenceEngines,
  listInferenceImages,
  type EngineOut,
  type ImageEntryOut,
} from '@/api/inference-image'
import { listModels, type ModelData } from '@/api/model'
import {
  getClusterTopology,
  type ClusterTopology,
  type TopologyGpu,
  type TopologyNode,
} from '@/api/topology'
import { useStore as useDeployStore } from '../useStore'

/**
 * 扁平 argv → 参数行(与后端三层合并器解析规则同口径):
 * token 以 `-` 开头即 flag,下一 token 存在且不以 `-` 开头则为其值,合并一行(`--model <path>`);
 * 否则为无值布尔 flag 或 `--k=v` 形式,独占一行。
 */
export function toArgLines(argv: string[]): string[] {
  const lines: string[] = []
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i]
    const next = argv[i + 1]
    if (token.startsWith('-') && next !== undefined && !next.startsWith('-')) {
      lines.push(`${token} ${next}`)
      i++
    } else {
      lines.push(token)
    }
  }
  return lines
}

/** 参数行 → 扁平 token:首个空白前为 flag(或 `--k=v` 整体),其后整体为值(保留值内空格);空行剔除 */
export function argLineToTokens(line: string): string[] {
  const trimmed = line.trim()
  if (!trimmed) return []
  const sepIndex = trimmed.search(/\s/)
  if (sepIndex === -1) return [trimmed]
  const value = trimmed.slice(sepIndex + 1).trim()
  return value ? [trimmed.slice(0, sepIndex), value] : [trimmed.slice(0, sepIndex)]
}

/**
 * 参数行是否命中系统必控 flag(前缀匹配):行首以 protected_flags 任一项开头即视为只读不可删,
 * 与后端「用户 extra_args 同名会被忽略」的拦截口径对齐(如 `--model /path` 命中 `--model`)。
 */
export function isProtectedArgLine(line: string, protectedFlags: string[]): boolean {
  const trimmed = line.trim()
  if (!trimmed) return false
  return protectedFlags.some((flag) => trimmed.startsWith(flag))
}

/** 启动参数推荐选项(AutoComplete 下拉项);type alias 以兼容 naive-ui option 的索引签名 */
export type ArgSuggestionOption = {
  /** 选中后填入输入框的参数行(naive-ui AutoComplete 按 label 填充,必须为纯参数行) */
  label: string
  value: string
  /**
   * 下拉项自定义渲染(hint 说明仅展示,不影响填充值)。
   * 必须基于 info.node 重建:naive-ui 的 option 样式与点击/悬停处理都挂在默认 node 上,
   * 直接返回裸节点会丢失样式且点击无效。
   */
  render?: (info: { node: VNode; selected: boolean }) => VNodeChild
}

/**
 * 引擎推荐参数(param_suggestions,后端自由对象 {name/hint/default/choices}) → AutoComplete 选项:
 * 有 choices 逐值展开,否则用 default,都无则仅 flag 本身。
 * label/value 为纯参数行(`--flag value`);hint 中文仅在 dropdown 内右侧展示,不参与填充。
 * name 缺失的项跳过。
 */
export function paramSuggestionOptions(
  suggestions: Record<string, unknown>[] | null | undefined,
): ArgSuggestionOption[] {
  if (!suggestions?.length) return []
  const options: ArgSuggestionOption[] = []
  for (const raw of suggestions) {
    const name = typeof raw.name === 'string' ? raw.name.trim() : ''
    if (!name) continue
    const hint = typeof raw.hint === 'string' && raw.hint.trim() ? raw.hint.trim() : ''
    const choices = Array.isArray(raw.choices) ? raw.choices : []
    const defaultText =
      raw.default === null || raw.default === undefined ? '' : String(raw.default).trim()
    const values = choices.length
      ? choices
          .map((choice) => (choice === null || choice === undefined ? '' : String(choice).trim()))
          .filter(Boolean)
      : defaultText
        ? [defaultText]
        : []
    const lines = values.length ? values.map((value) => `${name} ${value}`) : [name]
    for (const value of lines) {
      options.push({
        label: value,
        value,
        ...(hint
          ? {
              // 同 type/props 重建 node 以保留内置样式与点击处理,hint 作为追加子项右对齐展示;
              // z-1 与内置 __content 同层:悬浮态 option::before 的 pending 背景层是绝对定位,
              // 会盖住无 z-index 的 flex 子项(flex item 的 z-index 对 static 定位同样生效)
              render: (info) =>
                // node 由 naive-ui 以 h("div", ...) 创建,type 运行时必为元素标签,cast 收窄 VNodeTypes;
                // children 含 Slots 形态时不在数组子节点类型内,cast 收窄为 VNodeChild
                h(info.node.type as string, info.node.props, [
                  info.node.children as VNodeChild,
                  h(
                    'span',
                    { class: 'z-1 ml-auto text-small text-secondary shrink-0 pl-[1rem]' },
                    hint,
                  ),
                ]),
            }
          : {}),
      })
    }
  }
  return options
}

/** 向导表单值(modelId 对齐创建接口的 model_id) */
export interface WizardFormValues {
  /** 部署名称 */
  name: string
  /** 已上架模型主键 */
  modelId: number | null
  /** 推理引擎;auto 表示按 inspect 推荐 */
  engine: string
  /** 镜像版本(= catalog engine_version;null 由后端按默认版本解析,提交时作 spec.image_tag) */
  imageTag: string | null
  /** 最大上下文长度;默认 8K,null 按模型推荐,非空随预检传 topology.max_model_len */
  maxModelLen: number | null
  /** 目标集群主键 */
  clusterId: number | null
  /** 副本份数;Step2 副本卡片栏增删的直接回写,外部写该值时按长度截断/补空份 */
  replicas: number
  /** KV cache 精度档(引擎原生值;空串=默认不量化);选中档的 args 随预检传 engine_params */
  kvCacheDtype: string
}

/** 发布配置表单值(向导 Step3;空值语义=跟随模型/不计费) */
export interface WizardPublishValues {
  /** 部署达成后自动发布对外路由;关闭则只部署不发布(端点仍建,路由/绑定不建) */
  autoPublish: boolean
  /** 对外服务名(租户调用 model= 字段);空串=跟随模型名 */
  name: string
  /** 模型类别;null=跟随模型 */
  modelType: string | null
  /** 模型描述(模型中心展示) */
  description: string
  /** 能力标签(受控词表 ModelTag) */
  tags: string[]
  /** 输入定价(每百万 token / 元);null=不计费 */
  inputPrice: number | null
  /** 输出定价(每百万 token / 元);null=不计费 */
  outputPrice: number | null
  /** 缓存命中输入定价(每百万 token / 元);null=不计费 */
  cachedInputPrice: number | null
}

/** 表单值 → spec.publish(关闭自动发布时只带 auto_publish=false,其余字段无意义不传) */
export function buildPublishSpec(values: WizardPublishValues): PublishConfig {
  if (!values.autoPublish) return { auto_publish: false }
  const name = values.name.trim()
  const description = values.description.trim()
  return {
    auto_publish: true,
    ...(name ? { name } : {}),
    ...(values.modelType ? { model_type: values.modelType } : {}),
    ...(description ? { description } : {}),
    ...(values.tags.length ? { tags: values.tags } : {}),
    ...(values.inputPrice !== null ? { input_price_per_mtok: values.inputPrice } : {}),
    ...(values.outputPrice !== null ? { output_price_per_mtok: values.outputPrice } : {}),
    ...(values.cachedInputPrice !== null
      ? { cached_input_price_per_mtok: values.cachedInputPrice }
      : {}),
  }
}

/** 集群下拉选项:携带原始 ClusterData 供概要块/确认页使用 */
export interface ClusterOption {
  label: string
  value: number
  raw: ClusterData
}

/** 模型下拉选项:携带原始 ModelData 供确认页回显 */
export interface ModelOption {
  label: string
  value: number
  raw: ModelData
}

/** 无自回归 KV cache 的模型类别(embedding/rerank):KV 精度控件对这些类别无意义,不渲染 */
const NO_KV_CACHE_TYPES = new Set(['embedding', 'rerank'])

const currentStep = ref(1)
const form = reactive<WizardFormValues>({
  name: '',
  modelId: null,
  engine: 'auto',
  imageTag: null,
  maxModelLen: 8 * 1024,
  clusterId: null,
  replicas: 1,
  kvCacheDtype: '',
})
/** 发布配置(Step3 表单态,提交时经 buildPublishSpec 挂到 spec.publish) */
const publish = reactive<WizardPublishValues>({
  autoPublish: true,
  name: '',
  modelType: null,
  description: '',
  tags: [],
  inputPrice: null,
  outputPrice: null,
  cachedInputPrice: null,
})
const clusterOptions = shallowRef<ClusterOption[]>([])
const clusterLoading = ref(false)
/** 引擎能力字典(按模型预取并缓存;引擎下拉过滤/支持性判定的事实源) */
const engines = shallowRef<EngineOut[]>([])
const enginesLoading = ref(false)
/** 字典对应的模型 id(null=未按模型判定;模型变化要重拉——后端按模型给 supports_model) */
const enginesModelId = ref<number | null>(null)
/** 引擎字典请求序号:并发切换模型只认最后一次响应 */
let enginesSeq = 0
const modelOptions = shallowRef<ModelOption[]>([])
const modelLoading = ref(false)
const topology = shallowRef<ClusterTopology | null>(null)
const topologyLoading = ref(false)
const topologyError = ref(false)
/** 部署预检结果(Step1→2 基础预检,Step2→3 全量预检后含 resolved/issues) */
const inspect = shallowRef<InspectDeploymentResult | null>(null)
const inspectLoading = ref(false)
const inspectError = ref(false)
/** 基础预检缓存键:modelId:clusterId:engine,一致则跳过重拉 */
const inspectKey = ref('')
/** 推荐预检请求序号:并发切换模型/上下文时只认最后一次响应,防止慢响应覆盖新结果 */
let inspectRecommendationSeq = 0
/** 基础预检请求序号:Step2 快速改副本数/上下文并发触发时只认最后一次响应 */
let inspectLoadSeq = 0

/** 镜像目录(Step1 镜像版本下拉数据源;按引擎缓存) */
const imageEntries = shallowRef<ImageEntryOut[]>([])
const imageEntriesLoading = ref(false)
/** imageEntries 对应的引擎(缓存键;空串表示未加载/加载失败,下次触发会重拉) */
const imageEntriesEngine = ref('')
/** 镜像版本请求序号:并发切换引擎只认最后一次响应 */
let imageVersionSeq = 0

/**
 * 逐副本选卡:每份一份有序 uuid 列表(份内顺序即点选顺序);卡片式副本的权威状态。
 * 画布选卡/全选/反选/框选均作用于「激活副本」(activeReplicaIndex),份间互斥
 * (一张卡最多属于一份,已属其它份的卡禁选)。
 */
const replicaSelections = shallowRef<string[][]>([[]])
/** 当前编辑的副本下标(0 起);切换副本卡片即切换画布选卡目标 */
const activeReplicaIndex = ref(0)
/**
 * 激活副本的选中集(可写视图:读 = 激活份集合,写 = 整份替换)。
 * 组件/测试沿用的稳定句柄;全量口径(提交/校验/总量)用 allSelectedUuids。
 */
const selectedGpuUuids = computed<Set<string>>({
  get: () => activeReplicaGpuUuids.value,
  set: (value) => {
    writeActiveReplica([...value])
  },
})
/** 全部副本选中并集(按份顺序拼接,去重保序);提交 gpu_uuids/派生拓扑/校验的数据源 */
const allSelectedUuids = computed(() => {
  const seen = new Set<string>()
  for (const uuids of replicaSelections.value) {
    for (const uuid of uuids) seen.add(uuid)
  }
  return seen
})

/** 副本份数上限(卡片增删与外部写 form.replicas 共用同一 clamp 口径) */
export const REPLICA_COUNT_MAX = 64

/** 多副本色盘:副本卡片色点与画布色环同源,按份序号循环 */
export const REPLICA_COLORS = ['#2080f0', '#7c3aed', '#f0a020', '#18a058']

/** 激活副本选中集(只读视图;写入口统一走 writeActiveReplica/selectedGpuUuids setter) */
const activeReplicaGpuUuids = computed(() => new Set(replicaSelections.value[activeReplicaIndex.value] ?? []))

/** 写激活副本(整份换新数组,shallowRef 整体换引用保证响应) */
function writeActiveReplica(uuids: string[]) {
  const next = [...replicaSelections.value]
  next[activeReplicaIndex.value] = uuids
  replicaSelections.value = next
}

/**
 * 副本份数双向对齐:卡片增删改份结构后回写 form.replicas(loadInspect 缓存键/预检负载用);
 * 外部直接写 form.replicas(兼容入口,如测试)按长度截断/补空份。sync:同步代码内立即可见。
 */
watch(
  () => form.replicas,
  (count) => {
    const target = Math.max(1, Math.min(REPLICA_COUNT_MAX, Math.trunc(count) || 1))
    if (target !== count) form.replicas = target
    if (target === replicaSelections.value.length) return
    const next = replicaSelections.value.slice(0, target)
    while (next.length < target) next.push([])
    replicaSelections.value = next
    // CPU 钉定/动过标记随份数尾部对齐(补 null/false);删中间份的 index 修正由 removeReplica splice 负责
    const nextCpu = cpuNodeSelections.value.slice(0, target)
    while (nextCpu.length < target) nextCpu.push(null)
    cpuNodeSelections.value = nextCpu
    const nextTouched = cpuTouched.value.slice(0, target)
    while (nextTouched.length < target) nextTouched.push(false)
    cpuTouched.value = nextTouched
    activeReplicaIndex.value = Math.min(activeReplicaIndex.value, target - 1)
  },
  { flush: 'sync' },
)
watch(
  replicaSelections,
  (list) => {
    if (form.replicas !== list.length) form.replicas = list.length
  },
  { flush: 'sync' },
)

/**
 * CPU 部署逐份钉定节点 id(与 replicaSelections 平行;null=该份未钉)。
 * 点节点卡片只为激活副本钉定落点,份间互不覆盖;同节点可承载多份(内存按份累加由后端记账)。
 */
const cpuNodeSelections = shallowRef<(string | null)[]>([null])
/** 各份是否被用户显式动过(点选/取消):自动回填只补未动过的空份,不顶回用户意图 */
const cpuTouched = shallowRef<boolean[]>([false])
/** 可编辑启动参数行(全量预检 resolved.args 按配对口径成行回显初始化,提交时拆回扁平 argv 作为 engine_params.extra_args) */
const editArgs = ref<string[]>([])
/** 全量预检回显的参数行基线(系统生成项):提交时剔除,只把用户新增/改过的行当 extra_args */
const resolvedArgBaseline = ref<string[]>([])
/** 可编辑环境变量(全量预检 resolved.env 回显初始化,提交时作为 engine_params.env) */
const editEnv = ref<{ key: string; value: string }[]>([])
/** 强制部署标记(Step2→3 预检存在 issues 且用户确认「强制部署」后置位,提交时带 force) */
const forced = ref(false)
/** 最近一次创建成功的部署记录(提交成功后暂存,退出向导时携带供列表层直接打开部署详情) */
const lastCreated = shallowRef<ModelDeploymentOut | null>(null)

/** 配对预览的份单元:一份副本的选卡视图(卡 uuid 为该份已选卡) */
export interface PairingPairPreview {
  /** 副本序号(人读从 1 起,展示为 r{seq}) */
  seq: number
  /** 份内卡分布的节点(单机份规则下恒 1 个) */
  nodeIds: string[]
  /** 份内架构(nvidia/cann;同份同架构) */
  arch: string
  /** 该份已选的卡 uuid */
  gpuUuids: string[]
  /** true 表示非空且份内单 (节点,架构) 桶(卡数一致性由 topologyRuleError 校验) */
  complete: boolean
}

/** 逐副本配对视图(画布色环/确认页分组数据源;份即副本卡片,无贪心重排) */
export interface PairingPreview {
  pairs: PairingPairPreview[]
  /** 非空且份内单桶的份数 */
  completeCount: number
}

export function useWizardStore() {
  const deployStore = useDeployStore()

  /**
   * 已被部署占用的 GPU uuid 集合(status-aware:只统计 running/creating 部署,
   * stopped/failed 未收敛的部署不构成占用——其显存声明已出账)。
   */
  const occupiedGpuUuids = computed(
    () =>
      new Set(
        (topology.value?.deployments ?? [])
          .filter((item) => item.status === 'running' || item.status === 'creating')
          .flatMap((item) => item.gpu_uuids),
      ),
  )

  /** 全卡可分配显存上限系数:平台账本口径,总声明恒 ≤ 0.9×总量 */
  const VRAM_ALLOCATABLE_RATIO = 0.9

  /** 逐卡占用告警视图(告警行/悬浮归因/空推荐出路提示共用);无占用时为 null */
  const gpuOccupancyMap = computed(() => {
    const map = new Map<string, { declaredGb: number; allocatableGb: number; names: string[] }>()
    for (const gpu of inspect.value?.cluster_gpus ?? []) {
      const declared = gpu.declared_vram_gb ?? 0
      if (declared <= 0) continue
      const allocatable = Math.max(0, gpu.total_vram_gb * VRAM_ALLOCATABLE_RATIO - declared)
      const names = (gpu.occupied_by ?? [])
        .map((item) => item.deployment_name || item.deployment_id)
        .filter(Boolean)
      map.set(gpu.uuid, { declaredGb: declared, allocatableGb: allocatable, names })
    }
    return map
  })

  /** inspect 逐卡评估:uuid → 评估项(灰化/承载力/分组) */
  const inspectGpuMap = computed(() => {
    const map = new Map<string, InspectClusterGpu>()
    for (const gpu of inspect.value?.cluster_gpus ?? []) map.set(gpu.uuid, gpu)
    return map
  })

  /** inspect 逐节点可选性:node_id → 评估项(引擎+镜像 tag 视角) */
  const nodeSupportMap = computed(() => {
    const map = new Map<string, NodeSupportInfo>()
    for (const item of inspect.value?.node_support ?? []) map.set(item.node_id, item)
    return map
  })

  /** 生效引擎:手动值优先,auto 取 inspect 推荐 */
  const effectiveEngine = computed(() =>
    form.engine === 'auto' ? inspect.value?.recommendation?.engine : form.engine,
  )

  /** 生效引擎是否 llamacpp(GGUF 单机引擎:不支持跨机部署,前端选卡口径收紧) */
  const isLlamaCpp = computed(() => effectiveEngine.value === 'llamacpp')

  /**
   * 生效引擎是否支持多机部署:三级回落的事实源——
   * 一级 inspect 推荐(后端按所钉 image_tag 版本折算,最准)→ 二级引擎能力字典
   * (/engines,引擎级无版本感知)→ 三级宽松兜底 true(后端预检 pp 门控最终把关)。
   * llama.cpp 单机引擎恒 false。
   */
  const engineSupportsMultinode = computed(() => {
    if (isLlamaCpp.value) return false
    const fromInspect = inspect.value?.recommendation?.supports_multinode
    if (typeof fromInspect === 'boolean') return fromInspect
    const fromDict = engines.value.find(
      (item) => item.engine === effectiveEngine.value,
    )?.supports_multinode
    if (typeof fromDict === 'boolean') return fromDict
    return true
  })

  /**
   * 生效引擎的并行词表(tp/pp/ep 白名单):与 engineSupportsMultinode 同一套三级回落,
   * 宽松兜底视为三词全含(旧后端无字段时不阻断新形态)。
   */
  const effectiveParallel = computed(() => {
    const fromInspect = inspect.value?.recommendation?.supported_parallel
    if (fromInspect?.length) return fromInspect
    const fromDict = engines.value.find(
      (item) => item.engine === effectiveEngine.value,
    )?.supported_parallel
    if (fromDict?.length) return fromDict
    return ['tp', 'pp', 'ep']
  })

  /** 已选卡全为昇腾(cann 软件栈)时置 true:昇腾不支持多机,跨节点选集收敛单节点 */
  const selectionIsCann = computed(() => {
    const uuids = allSelectedUuids.value
    if (!uuids.size) return false
    for (const gpu of topology.value?.gpus ?? []) {
      if (uuids.has(gpu.uuid) && gpu.arch !== 'cann') return false
    }
    return true
  })

  /**
   * 跨机 PP 形态可用:引擎支持多机 ∧ 词表含 pp ∧ 选卡非 cann。
   * 为 true 时单副本跨节点选卡组装「节点内 TP + 跨机 PP」拓扑,否则维持旧形态跨机 TP。
   */
  const crossNodePpAvailable = computed(
    () =>
      engineSupportsMultinode.value &&
      effectiveParallel.value.includes('pp') &&
      !selectionIsCann.value,
  )

  /**
   * 须收敛到单节点的语境:llamacpp(后端 validate_cross_node 全署单节点)/
   * 引擎(镜像版本)不支持多机 / 昇腾选卡。为 true 时跨节点选集自动收敛单节点。
   */
  const crossNodePpBlocked = computed(
    () => isLlamaCpp.value || !engineSupportsMultinode.value || selectionIsCann.value,
  )

  /** 所选模型(引擎服务能力判定的输入) */
  const selectedModel = computed(
    () => modelOptions.value.find((item) => item.value === form.modelId)?.raw ?? null,
  )

  /**
   * 生效引擎能否服务所选模型(格式 + 类别):能力字典视角,与后端
   * `EngineAdapter.supported_formats/supported_categories` 同源(如 llama.cpp 仅 gguf)。
   * 引擎/模型未选或字段缺失时放宽该维度(不误禁)。
   */
  const engineServesModel = computed(() => {
    const engine = engines.value.find((item) => item.engine === effectiveEngine.value)
    if (!engine) return true
    // 后端按模型判定优先(与部署预检同源)
    if (typeof engine.supports_model === 'boolean') return engine.supports_model
    // 兜底:字典未按模型判定(未带 model_id)时按能力字典本地判
    const model = selectedModel.value
    if (!model) return true
    const formatOk = !model.format || (engine.supported_formats ?? []).includes(model.format)
    const categoryOk =
      !model.model_type || (engine.supported_categories ?? []).includes(model.model_type)
    return formatOk && categoryOk
  })

  /** 生效引擎是否支持 CPU 部署(能力字典 supported_gpu_archs 含 cpu + 引擎能服务该模型) */
  const engineSupportsCpu = computed(
    () =>
      engineServesModel.value &&
      (engines.value
        .find((item) => item.engine === effectiveEngine.value)
        ?.supported_gpu_archs?.includes('cpu') ??
        false),
  )

  /** CPU 部署模式:任一份已钉定 CPU 节点(与 GPU 选卡互斥;各份可钉不同节点) */
  const cpuMode = computed(() => cpuNodeSelections.value.some((nodeId) => nodeId !== null))
  /** CPU 逐份钉定配齐:每份都已钉节点(payload 发 node_ids 的门控;topologyRuleError 未齐报错) */
  const cpuPinComplete = computed(() =>
    cpuNodeSelections.value.length > 0 && cpuNodeSelections.value.every((nodeId) => nodeId !== null),
  )
  /**
   * 首个已钉节点 id(单值消费方兼容视图:确认页/旧读点)。
   * cpuMode 下至少一份已钉,故非 null;未钉任何份时为 null。
   */
  const selectedCpuNodeId = computed(() => cpuNodeSelections.value.find((nodeId) => nodeId !== null) ?? null)
  /** 激活副本已钉的节点 id(节点卡片选中态/点选切换口径;null=激活份未钉) */
  const activeReplicaCpuNodeId = computed(
    () => cpuNodeSelections.value[activeReplicaIndex.value] ?? null,
  )

  /** 生效引擎的 KV cache 精度选项(后端 adapter 声明的支持矩阵;空表=无此能力,不渲染控件) */
  const kvCacheOptions = computed(
    () =>
      engines.value.find((item) => item.engine === effectiveEngine.value)?.kv_cache_dtype_options ??
      [],
  )

  /** 当前选中的 KV 精度项(未选/引擎切换后失效 → null) */
  const kvCacheOption = computed(
    () => kvCacheOptions.value.find((item) => item.value === form.kvCacheDtype) ?? null,
  )

  /** 选中档要写入 engine_params.extra_args 的原生 flag(默认档为空表) */
  const kvCacheExtraArgs = computed(() => kvCacheOption.value?.args ?? [])

  /** KV 精度下拉选项:label 由后端给,倍率 <1 时补占用标注(如「FP8 E4M3(KV 约 0.5×)」) */
  const kvCacheSelectOptions = computed(() =>
    kvCacheOptions.value.map((option) => ({
      label: option.kv_ratio < 1 ? `${option.label}(KV 约 ${option.kv_ratio}×)` : option.label,
      value: option.value,
    })),
  )

  /** 所选模型是否有自回归 KV cache(embedding/rerank 无,不渲染精度控件;与后端 ModelType.has_kv_cache 同口径) */
  const hasKvCache = computed(() => {
    const modelType = modelOptions.value.find((item) => item.value === form.modelId)?.raw.model_type
    return !modelType || !NO_KV_CACHE_TYPES.has(modelType)
  })

  /**
   * 预检拓扑中的引擎项(protected_flags/param_suggestions 事实源):
   * 优先匹配生效引擎,匹配不到回退首项(后端通常仅返回当前引擎一条)。
   */
  const previewEngine = computed(() => {
    const engineList = inspect.value?.preview_topology?.engines ?? []
    if (!engineList.length) return null
    return engineList.find((item) => item.engine === effectiveEngine.value) ?? engineList[0]
  })

  /** 系统必控 flag:命中前缀的参数行只读不可删 */
  const protectedFlags = computed(() => previewEngine.value?.protected_flags ?? [])

  /** 启动参数推荐选项(AutoComplete 下拉数据源) */
  const argSuggestionOptions = computed(() =>
    paramSuggestionOptions(previewEngine.value?.param_suggestions),
  )

  /** 参数行是否只读(命中系统必控 flag 前缀) */
  function isArgProtected(line: string): boolean {
    return isProtectedArgLine(line, protectedFlags.value)
  }

  /**
   * 加载引擎能力字典:按模型缓存(modelId 变化必须重拉——后端按该模型判定 supports_model)。
   * 不传 modelId(进页面预取)时只拿能力,不判定支持性(前端放宽,不误灰)。
   */
  async function loadEngines(modelId: number | null = form.modelId) {
    if (engines.value.length && enginesModelId.value === modelId) return
    const seq = ++enginesSeq
    enginesLoading.value = true
    try {
      const list = await listInferenceEngines(modelId ?? undefined)
      if (seq !== enginesSeq) return
      engines.value = list
      enginesModelId.value = modelId
    } finally {
      if (seq === enginesSeq) enginesLoading.value = false
    }
  }

  /**
   * 拉取指定引擎的镜像目录(Step1 镜像版本下拉);同引擎已缓存则跳过。
   * 失败清空并静默(HTTP 层已提示),缓存键置空使下次触发可重试。
   */
  async function loadImageVersions(engine: string) {
    if (imageEntriesEngine.value === engine) return
    const seq = ++imageVersionSeq
    imageEntriesLoading.value = true
    try {
      const entries = await listInferenceImages({ engine })
      if (seq !== imageVersionSeq) return
      imageEntries.value = entries
      imageEntriesEngine.value = engine
    } catch {
      if (seq !== imageVersionSeq) return
      imageEntries.value = []
      imageEntriesEngine.value = ''
    } finally {
      if (seq === imageVersionSeq) imageEntriesLoading.value = false
    }
  }

  /** 引擎展示文案:取能力字典 display_name(字典未加载/未知引擎回退原始值;auto 为自动选择) */
  function engineLabel(value: string): string {
    if (value === 'auto') return '自动选择'
    return engines.value.find((item) => item.engine === value)?.display_name ?? value
  }

  /**
   * 引擎下拉选项:能力字典本地过滤(deployable);**后端按模型判定的不支持项置灰不可选**
   * (supports_model=false + unsupported_reason 作为标签后缀),避免到 Step2/预检才报错。
   * inspect 只给推荐引擎给不了可选集合,可选集合只能从字典派生。
   */
  function engineOptionsFor() {
    const available = engines.value.filter((item) => item.deployable)
    return [
      { label: engineLabel('auto'), value: 'auto' },
      ...available.map((item) => {
        const unsupported = item.supports_model === false
        return {
          label: unsupported
            ? `${item.display_name}（${item.unsupported_reason || '不支持该模型'}）`
            : item.display_name,
          value: item.engine,
          ...(unsupported ? { disabled: true } : {}),
        }
      }),
    ]
  }

  /** 加载 active 集群选项(Step1 目标集群下拉) */
  async function loadClusters() {
    clusterLoading.value = true
    try {
      const res = await listClusters({ status: 'active', page: 1, page_size: 100 })
      clusterOptions.value = res.items.map((item) => ({
        label: `${item.name}(${item.node_count}节点)`,
        value: item.id,
        raw: item,
      }))
    } finally {
      clusterLoading.value = false
    }
  }

  /** 加载已上架模型选项(Step1 模型下拉);值取模型主键,与创建接口 model_id 对齐 */
  async function loadModels() {
    modelLoading.value = true
    try {
      const res = await listModels({ page: 1, page_size: 100 })
      modelOptions.value = res.items.map((item) => ({
        label: item.name,
        value: item.id,
        raw: item,
      }))
    } finally {
      modelLoading.value = false
    }
  }

  /** 拉取当前目标集群拓扑;同集群已缓存则跳过,force 强制重拉(Step2 重试) */
  async function loadTopology(force = false) {
    if (!form.clusterId) return
    if (!force && topology.value?.cluster.id === form.clusterId) return
    topologyLoading.value = true
    topologyError.value = false
    try {
      topology.value = await getClusterTopology(form.clusterId)
    } catch {
      // 接口异常已由 HTTP 层统一提示,这里只落失败态供步骤内展示重试
      topology.value = null
      topologyError.value = true
    } finally {
      topologyLoading.value = false
    }
  }

  /**
   * 模型推荐预检(Step1 选模型即调,仅传 model_id):拿推荐引擎/集群用于自动回填。
   * 最大上下文在 Step2 编辑,调整走全量预检(loadInspect)重估显存,此处不携带。
   * 失败返回 null(HTTP 层已统一提示),不阻塞手选;结果不写缓存键,Step1→2 仍按完整键重拉。
   */
  async function inspectRecommendation(modelId: number): Promise<InspectDeploymentResult | null> {
    const seq = ++inspectRecommendationSeq
    try {
      const result = await inspectDeployment({ model_id: modelId })
      // 竞态守卫:请求期间又发起了新的推荐预检,丢弃本次结果(返回 null 让调用方跳过回填)
      if (seq !== inspectRecommendationSeq) return null
      inspect.value = result
      inspectKey.value = ''
      inspectError.value = false
      return result
    } catch {
      return null
    }
  }

  /**
   * 基础部署预检(仅 model_id+cluster_id+可选 engine):拿推荐引擎/拓扑与逐卡评估。
   * 容错同 loadTopology:失败落 inspectError,Step2 展示告警并重试,不阻塞推进。
   */
  async function loadInspect(force = false) {
    if (form.modelId === null || form.clusterId === null) return
    // 份数参与逐份规划(replica_plans 按请求拓扑展开),必须进缓存键——副本数在 Step2 改过
    // 后,复用旧缓存会给出单份落点(每份的卡与推荐总卡数都不随份数刷新);
    // KV 精度影响显存估算与推荐落点,同键;CPU 钉定集(配齐时)整体进键——改任一份落点都要按新拓扑重检;
    // 镜像版本进键——recommendation 的能力折算(supported_parallel/supports_multinode)按 image_tag
    // 版本感知,换版本不重检会拿陈旧能力把拓扑组装错。
    const key = `${form.modelId}:${form.clusterId}:${form.engine}:${form.imageTag ?? ''}:${form.maxModelLen ?? ''}:${Math.max(1, form.replicas)}:${form.kvCacheDtype}:${cpuMode.value && cpuPinComplete.value ? cpuNodeSelections.value.join(',') : ''}`
    if (!force && inspect.value && inspectKey.value === key) return
    const seq = ++inspectLoadSeq
    inspectLoading.value = true
    inspectError.value = false
    try {
      const result = await inspectDeployment({
        model_id: form.modelId,
        cluster_id: form.clusterId,
        engine: form.engine === 'auto' ? undefined : form.engine,
        topology: {
          replicas: Math.max(1, form.replicas),
          ...(form.maxModelLen ? { max_model_len: form.maxModelLen } : {}),
        },
        // 已选 KV 精度档随预检传入:后端按字节数折算 KV 项,需显存/推荐落点实时跟随
        ...(kvCacheExtraArgs.value.length
          ? { engine_params: { extra_args: [...kvCacheExtraArgs.value] } }
          : {}),
        // CPU 部署(任一份已钉):授权 CPU 调度 + KV 落内存;配齐发逐份钉定(node_ids[i]=第 i 份落点),
        // 未齐不发 selector(自动 binpack 口径,replica_plans 即空份回填的推荐落点)。
        // offload_preference 必须显式 cpu_only——节点有可用卡时 auto 会解成显存档,与所选部署不符。
        // llama.cpp 未钉任何份时也带 allow_cpu(auto):拿逐层表与各节点可用内存,
        // 供「默认优先选中 CPU」挑落点;auto 只解两端(全量上卡 / 纯 CPU),不改显存档推荐。
        ...(cpuMode.value
          ? {
              allow_cpu: true,
              offload_preference: 'cpu_only' as const,
              ...(cpuPinComplete.value
                ? { gpu_selector: { node_ids: [...cpuNodeSelections.value] as string[] } }
                : {}),
            }
          : isLlamaCpp.value
            ? { allow_cpu: true }
            : {}),
        // 已选镜像版本随预检传入,resolved.image 按该版本解析,与最终部署一致
        image_tag: form.imageTag ?? undefined,
      })
      // 竞态守卫:请求期间又发起了新的预检(Step2 快速改副本数/上下文)则丢弃本次结果
      if (seq !== inspectLoadSeq) return
      inspect.value = result
      inspectKey.value = key
    } catch {
      if (seq !== inspectLoadSeq) return
      inspect.value = null
      inspectKey.value = ''
      inspectError.value = true
    } finally {
      if (seq === inspectLoadSeq) inspectLoading.value = false
    }
  }

  /**
   * 全量部署预检(Step2→3 门控):带 engine+topology+gpu_selector,回写 inspect 供确认页展示;失败返回 null。
   * 基线差集口径:resolved.args 回显里含「本轮由前端注入的用户层选项」(如 KV 精度 flag)——
   * 它们属于用户配置而非系统生成项,记录基线时必须剔除,否则提交差集会把它们当系统项
   * 静默丢弃(用户选的量化档 end-to-end 失效)。
   */
  async function runFinalInspect(): Promise<InspectDeploymentResult | null> {
    if (form.modelId === null || form.clusterId === null || !derivedTopology.value) return null
    // CPU 逐份钉定未配齐不发请求(正常路径被 Step2 校验拦下;此处兜底,防 null 混进 node_ids)
    if (cpuMode.value && !cpuPinComplete.value) return null
    // 使在途基础预检失效:防抖中的 loadInspect 迟到响应不得覆盖全量结果
    ++inspectLoadSeq
    inspectLoading.value = true
    // 每次全量预检重置强制标记,存在 issues 时由控制器确认后再置回
    forced.value = false
    try {
      const result = await inspectDeployment({
        model_id: form.modelId,
        cluster_id: form.clusterId,
        engine: effectiveEngine.value,
        topology: derivedTopology.value,
        // CPU 部署(配齐)逐份钉节点(node_ids[i]=第 i 份落点);GPU 部署按全部副本所选卡并集(份序拼接)。
        // cpuMode 未配齐走不到这里(Step2 topologyRuleError 已拦),守卫兜底防 null 漏发。
        ...(cpuMode.value
          ? {
              gpu_selector: { node_ids: [...cpuNodeSelections.value] as string[] },
              allow_cpu: true,
              offload_preference: 'cpu_only' as const,
            }
          : { gpu_selector: { gpu_uuids: [...allSelectedUuids.value] } }),
        // 已选 KV 精度档随全量预检传入:resolved.args 会回显对应 flag,Step3 参数列表可继续编辑
        ...(kvCacheExtraArgs.value.length
          ? { engine_params: { extra_args: [...kvCacheExtraArgs.value] } }
          : {}),
        image_tag: form.imageTag ?? undefined,
      })
      // 纯 CPU 部署:该节点内存装不下时后端按偏好回退到显存档(静默改用 GPU),与所选不符。
      // 前置成阻断 issue,由 Step2→3 确认弹窗让用户改选节点或显式强制部署。
      if (cpuMode.value && result.memory && result.memory.scenario !== 'cpu_only') {
        const need = result.memory.tiers[result.memory.recommended_tier]?.ram_gb
        result.deployable = false
        result.issues = [
          `该节点内存装不下纯 CPU 部署${need ? `(需约 ${need}GB)` : ''},会改用显存档;请更换节点或改走显存部署`,
          ...result.issues,
        ]
      }
      inspect.value = result
      // 最终解析回显进可编辑列表(按约定口径配对成行:flag 与值合并一行),Step3 逐行增删改
      editArgs.value = result.resolved ? toArgLines(result.resolved.args) : []
      // 记录基线 = 系统生成项(如 --model 占位路径/--port):提交时只回传用户新增/改过的行。
      // 剔除本轮由前端注入的 KV 精度 flag 行——后端三层合并会把它回显进 resolved.args,
      // 但它是用户选项(须随提交回传),混入基线会被差集过滤掉(该功能端到端失效)。
      const injectedLines = new Set(toArgLines(kvCacheExtraArgs.value).map((line) => line.trim()))
      resolvedArgBaseline.value = editArgs.value.filter((line) => !injectedLines.has(line.trim()))
      editEnv.value = result.resolved
        ? Object.entries(result.resolved.env).map(([key, value]) => ({ key, value }))
        : []
      return result
    } catch {
      // 接口异常已由 HTTP 层统一提示,返回 null 让控制器停留本步
      return null
    } finally {
      inspectLoading.value = false
    }
  }

  /** 激活副本已选卡分布的节点 id 集合(单副本跨机推导/校验口径) */
  const selectedNodeIds = computed(() => {
    const ids = new Set<string>()
    for (const gpu of topology.value?.gpus ?? []) {
      if (activeReplicaGpuUuids.value.has(gpu.uuid)) ids.add(gpu.node_id)
    }
    return ids
  })

  /** 全部副本已选卡分布的节点 id 集合(llama.cpp 全局单节点约束口径,与后端 validate_cross_node 一致) */
  const allSelectedNodeIds = computed(() => {
    const ids = new Set<string>()
    for (const gpu of topology.value?.gpus ?? []) {
      if (allSelectedUuids.value.has(gpu.uuid)) ids.add(gpu.node_id)
    }
    return ids
  })

  /**
   * 单机语境的锁定节点(任一份有已选卡后,后续选卡只允许该节点的约束锚点):
   * llama.cpp = 部署级锁(全部署只允许同一节点,后端 validate_cross_node 按整个卡集合校验);
   * 其余须单机语境(引擎无多机/cann)= 激活份已确立节点(份级锁,份间可各占不同单节点)。
   * 非须单机语境恒为 null。
   */
  const singleNodeLockedNodeId = computed(() => {
    if (!crossNodePpBlocked.value) return null
    const ids = isLlamaCpp.value ? allSelectedNodeIds.value : selectedNodeIds.value
    return ids.size > 0 ? [...ids][0] : null
  })

  /** 锁定节点的主机名(禁选原因文案用);节点查不到时回退节点 id */
  const singleNodeLockedHostname = computed(() => {
    const nodeId = singleNodeLockedNodeId.value
    if (!nodeId) return ''
    return topology.value?.nodes.find((node) => node.id === nodeId)?.hostname ?? nodeId
  })

  /**
   * 须单机语境的人读原因(禁选/校验文案按语境分流):
   * llama / 昇腾(cann)选卡 / 引擎或镜像版本不支持多机。
   */
  const singleNodeLockReason = computed(() => {
    if (isLlamaCpp.value) return 'llama.cpp 仅支持单节点部署'
    if (selectionIsCann.value) return '昇腾(cann)卡不支持多机部署'
    return '当前引擎或镜像版本不支持多机部署'
  })

  /**
   * uuid → 所属副本序号(1 起;未选任何份为 null)。份间互斥的禁选与角标数据源。
   */
  const replicaIndexOfUuid = computed(() => {
    const map = new Map<string, number>()
    replicaSelections.value.forEach((uuids, index) => {
      for (const uuid of uuids) map.set(uuid, index + 1)
    })
    return map
  })

  /** gpu uuid → 拓扑记录(可选性判断按 uuid 寻址,避免反复 find) */
  const gpuByUuid = computed(
    () => new Map((topology.value?.gpus ?? []).map((gpu) => [gpu.uuid, gpu] as const)),
  )

  /**
   * Step2 规格行形态提示:单副本选卡跨节点(各节点均分)时亮出将提交的拓扑形态——
   * PP 可用 = 节点内 TP + 跨机 PP,否则 = 跨机 TP(旧形态)。其余语境空串不渲染。
   */
  const crossNodeModeTip = computed(() => {
    if (cpuMode.value || replicaSelections.value.length > 1) return ''
    const total = activeReplicaGpuUuids.value.size
    const nodeCount = selectedNodeIds.value.size
    if (nodeCount <= 1 || total % nodeCount !== 0) return ''
    // 须单机语境的瞬态跨节点(收敛 watch 即将处理)不亮跨机 TP 误导文案
    if (crossNodePpBlocked.value) return ''
    return crossNodePpAvailable.value
      ? `跨 ${nodeCount} 节点 · 节点内 TP + 跨机 PP(每节点 ${total / nodeCount} 卡)`
      : `跨 ${nodeCount} 节点 · 跨机 TP`
  })

  /** 单副本模式:整个选卡集合须同组才能同 TP;多副本只要求每份内同节点同架构,份间可异构 */
  const singleCopyMode = computed(() => replicaSelections.value.length <= 1)

  /**
   * 激活副本内已确立的 (node, arch) 桶:非空即约束激活份后续只能选同桶卡
   * (多副本每份须同节点同架构;份间可异构)。单副本(可跨机)恒为 null。
   */
  const activeReplicaBucket = computed<string | null>(() => {
    if (singleCopyMode.value) return null
    for (const uuid of activeReplicaGpuUuids.value) {
      const gpu = gpuByUuid.value.get(uuid)
      if (!gpu) continue
      return `${gpu.node_id}|${gpu.arch}`
    }
    return null
  })

  /**
   * 拓扑按选卡自动推导:多副本时每份卡数 = 首份卡数(份间须一致,校验见 topologyRuleError),
   * nnodes 为每副本跨的节点数——多副本时每份卡须同节点(单机份),nnodes 恒 1;
   * 单副本跨机手选时按引擎能力分流:PP 可用 = 节点内 TP + 跨机 PP(tp=每节点卡数/pp=节点数),
   * 否则维持旧形态跨机 TP(tp=总卡数/pp=1)。max_model_len 用户指定优先,否则取 inspect 推荐值。
   */
  const derivedTopology = computed(() => {
    const maxModelLen =
      form.maxModelLen ?? inspect.value?.recommendation?.topology.max_model_len ?? null
    const copies = replicaSelections.value.length
    // CPU 部署:无卡,每副本=该节点一个引擎实例(tp=pp=nnodes=1),份数=副本数
    if (cpuMode.value) {
      return {
        tp: 1,
        pp: 1,
        replicas: copies,
        nnodes: 1,
        max_model_len: maxModelLen,
      }
    }
    const first = replicaSelections.value[0]?.length ?? 0
    if (!first) return null
    const nodeCount = selectedNodeIds.value.size
    // 单副本跨机且 PP 形态可用(各节点均分由跨机校验保证,整除守卫兜底):
    // 节点内 TP + 跨机 PP,每层 allreduce 不走网络,只在 pipeline stage 边界传激活
    if (copies <= 1 && nodeCount > 1 && crossNodePpAvailable.value && first % nodeCount === 0) {
      return {
        tp: first / nodeCount,
        pp: nodeCount,
        replicas: copies,
        nnodes: nodeCount,
        max_model_len: maxModelLen,
      }
    }
    return {
      tp: first,
      pp: 1,
      replicas: copies,
      nnodes: copies > 1 ? 1 : nodeCount,
      max_model_len: maxModelLen,
    }
  })

  /**
   * 拓扑约束校验;null 表示合法,否则为提示文案。
   * 多副本:每份非空、份内同节点同架构(先报,可指引移除外来卡)、卡数一致;
   * 单副本:沿用整除 + 各节点均分(跨机)与同组约束。
   */
  const topologyRuleError = computed(() => {
    // CPU 部署:无卡,GPU 侧规则(均分/配对)不适用;但须逐份配齐钉定节点
    if (cpuMode.value) {
      const emptySeq = cpuNodeSelections.value.findIndex((nodeId) => nodeId === null)
      if (emptySeq >= 0) return `副本${emptySeq + 1} 还未选择 CPU 落点节点`
      return null
    }
    const copies = replicaSelections.value.length
    // llamacpp 单机引擎:全部署(所有份合并)的卡必须同节点(与后端 validate_cross_node 口径一致)
    if (isLlamaCpp.value && allSelectedNodeIds.value.size > 1) {
      return 'llama.cpp 引擎不支持跨机部署,请将所选卡收敛到同一节点'
    }
    // 其余须单机语境(引擎无多机/cann):单副本跨节点非法(收敛 watch 通常已即时收敛,此处兜底)
    if (!isLlamaCpp.value && crossNodePpBlocked.value && selectedNodeIds.value.size > 1) {
      return `${singleNodeLockReason.value},请将所选卡收敛到同一节点`
    }
    if (copies > 1) {
      const perCopy = replicaSelections.value[0]?.length ?? 0
      if (!perCopy) {
        return `副本1 还未选卡(每份至少 1 张)`
      }
      const emptySeq = replicaSelections.value.findIndex((uuids) => !uuids.length)
      if (emptySeq >= 0) {
        return `副本${emptySeq + 1} 还未选卡(每份至少 1 张)`
      }
      // 份内同节点同架构兜底:先于卡数校验——先报桶违规可指引移除外来卡(否则提示「补齐卡数」
      // 与真实原因错位,而外来卡已不可点,用户会卡死)
      const badPair = replicaSelections.value.findIndex((uuids) => {
        const keys = new Set(
          uuids.map((uuid) => {
            const gpu = gpuByUuid.value.get(uuid)
            return gpu ? `${gpu.node_id}|${gpu.arch}` : ''
          }),
        )
        return keys.size > 1
      })
      if (badPair >= 0) {
        return `副本${badPair + 1} 的卡须在同一节点且同架构`
      }
      const mismatched = replicaSelections.value.findIndex((uuids) => uuids.length !== perCopy)
      if (mismatched >= 0) {
        return `各副本卡数须一致(副本1 已选 ${perCopy} 张,副本${mismatched + 1} 为 ${replicaSelections.value[mismatched].length} 张)`
      }
      return null
    }
    // 单副本
    const total = activeReplicaGpuUuids.value.size
    if (!total) return null
    if (selectedNodeIds.value.size > 1) {
      // 单副本跨机:卡数整除节点数且各节点均分
      const nnodes = selectedNodeIds.value.size
      if (total % nnodes !== 0) {
        return `跨节点部署需卡数整除节点数(当前 ${total} 张卡分布在 ${nnodes} 个节点)`
      }
      const perNode = total / nnodes
      for (const nodeId of selectedNodeIds.value) {
        const count = (topology.value?.gpus ?? []).filter(
          (gpu) => gpu.node_id === nodeId && activeReplicaGpuUuids.value.has(gpu.uuid),
        ).length
        if (count !== perNode) return `跨节点部署需各节点卡数一致(每节点 ${perNode} 张)`
      }
    }
    return null
  })

  /** 逐副本配对视图(色环着色/确认页分组数据源);多副本非空份才有 pair,单副本 pairs 为空 */
  const pairingPreview = computed<PairingPreview>(() => {
    const pairs: PairingPairPreview[] = []
    replicaSelections.value.forEach((uuids, index) => {
      if (!uuids.length) return
      const nodeIds = new Set<string>()
      const archs = new Set<string>()
      for (const uuid of uuids) {
        const gpu = gpuByUuid.value.get(uuid)
        if (!gpu) continue
        nodeIds.add(gpu.node_id)
        archs.add(gpu.arch)
      }
      pairs.push({
        seq: index + 1,
        nodeIds: [...nodeIds],
        arch: [...archs][0] ?? '',
        gpuUuids: [...uuids],
        complete: nodeIds.size === 1 && archs.size === 1,
      })
    })
    return {
      pairs,
      completeCount: pairs.filter((pair) => pair.complete).length,
    }
  })

  /**
   * 推荐落点为空的人读原因(Step2 顶部报错):副本数/并行度超出集群空闲资源时后端
   * recommended_placement 为空且 issues 带分流文案,优先透传;文案缺失按请求拓扑口径
   * 兜底。已手选卡时不提示(配不齐由 topologyRuleError/pairingPreview 接管)。
   * 纯 CPU 档(scenario=cpu_only)空落点 = 内存容量阻断(钉定节点不足/多副本挤不下),
   * 透传后端 issues 文案,不走「空闲卡不足」误导口径。
   */
  const noPlacementReason = computed(() => {
    const result = inspect.value
    const placement = result?.recommended_placement
    if (!result || !placement) return null
    // 任一落点口径非空即有落点:GPU 卡 / 跨机节点集 / 逐份规划(纯 CPU 档走 node_ids+replica_plans)
    if (
      placement.gpu_uuids.length ||
      placement.node_ids.length ||
      placement.replica_plans?.length
    )
      return null
    // 已手选卡时不提示(基础预检未带选择口径,配不齐由 topologyRuleError/pairingPreview 接管);
    // cpuMode 与选卡互斥恒无卡,纯 CPU 空落点照常走到下方 cpu_only 分支
    if (allSelectedUuids.value.size) return null
    // 纯 CPU 档空落点:容量阻断(deployable=false),issues 必带原因(内存不足等)
    if (result.memory?.scenario === 'cpu_only') {
      const need = result.memory.tiers[0]?.ram_gb
      return (
        result.issues.find((text) => text.includes('内存')) ??
        result.issues[0] ??
        `集群节点可用内存不足,无法承载纯 CPU 部署${need ? `(需约 ${need}GB)` : ''};请释放内存或减少副本数`
      )
    }
    const fromIssues = result.issues.find(
      (text) => text.includes('空闲卡不足') || text.includes('提高 tp'),
    )
    const copies = Math.max(1, form.replicas)
    const total = placement.total_gpus ?? copies
    const reason =
      fromIssues ?? `当前副本配置共需 ${total} 张卡,集群空闲可选卡不足;请减少副本数、缩小并行度或释放占用卡`
    // 共用显存出路:集群存在「在线、无灰化、被运行中部署占用」的卡时,报错末尾给出手选出路
    // (避免「请释放占用卡」成为唯一出路);符合口径的卡数按 inspect 逐卡口径统计
    const colocatable = (result.cluster_gpus ?? []).filter(
      (gpu) => !gpu.grey_reason && gpu.status === 'online' && occupiedGpuUuids.value.has(gpu.uuid),
    ).length
    if (colocatable > 0) {
      return `${reason};也可手动选择被占用的 ${colocatable} 张卡共用显存(确认部署时调低显存占比)`
    }
    return reason
  })

  /**
   * 激活副本已确立的分组(同组才能同 TP):取份内首卡的 inspect group_key;
   * 空份/无评估数据时为 null(不约束)。单/多副本统一生效——每份内同 TP。
   */
  const selectedGroupKey = computed(() => {
    for (const uuid of activeReplicaGpuUuids.value) {
      const key = inspectGpuMap.value.get(uuid)?.group_key
      if (key) return key
    }
    return null
  })

  /**
   * 基础可选(节点/卡在线 + 未灰化 + llama 全局锁;不含占用与副本维度)。
   * 被占卡在此口径下可选——共用显存靠用户显式调低显存占比解锁,预检红由后端把关。
   */
  function isGpuBaseSelectable(gpu: TopologyGpu): boolean {
    const node = topology.value?.nodes.find((item) => item.id === gpu.node_id)
    if (node?.status !== 'online' || gpu.status !== 'online') {
      return false
    }
    if (singleNodeLockedNodeId.value && gpu.node_id !== singleNodeLockedNodeId.value) return false
    return !inspectGpuMap.value.get(gpu.uuid)?.grey_reason
  }

  /**
   * 批量口径:基础可选叠加未被运行中部署占用。
   * 全选/自动配对/反选/框选/点节点整选/推荐回填等批量入口一律排除被占卡——
   * 共用显存只能是用户逐张手选的显式意图,避免静默把副本落到别人正在用的卡上。
   */
  function isGpuBatchSelectable(gpu: TopologyGpu): boolean {
    return isGpuBaseSelectable(gpu) && !occupiedGpuUuids.value.has(gpu.uuid)
  }

  /** GPU 是否可手选:基础可选 + 未被其它副本选中 + 激活份桶/分组约束;已属激活份的卡永远可点(取消勾选优先) */
  function isGpuSelectable(gpu: TopologyGpu): boolean {
    if (!isGpuBaseSelectable(gpu)) return false
    // 份间互斥:已被**其它**副本选中的卡不可再选(激活份自身的卡保持可选,供取消勾选)
    const ownerSeq = replicaIndexOfUuid.value.get(gpu.uuid)
    if (ownerSeq !== undefined && ownerSeq !== activeReplicaIndex.value + 1) return false
    // 已属激活份的卡永远可再点(取消勾选):桶/组约束只约束"新选入",
    // 否则自动分配塞入的异桶卡(或组约束变化后)会无法移除,用户被卡死在 Step2
    if (ownerSeq === activeReplicaIndex.value + 1) return true
    // 多副本:份内卡须同节点同架构(激活份已确立桶后,不同桶的卡不可再选)
    if (activeReplicaBucket.value && `${gpu.node_id}|${gpu.arch}` !== activeReplicaBucket.value)
      return false
    // 同组才能同 TP:激活份已确立分组时,不同组的卡不可再选
    const assessed = inspectGpuMap.value.get(gpu.uuid)
    if (selectedGroupKey.value && assessed?.group_key && assessed.group_key !== selectedGroupKey.value)
      return false
    return true
  }

  /** grey_reason → 兜底文案(grey_detail 缺失时;未知枚举回退「不可用」) */
  const GREY_REASON_LABELS: Record<string, string> = {
    arch_mismatch: '架构不匹配',
    no_image: '无匹配镜像',
    offline: '离线',
  }

  /** inspect 禁选原因(优先于拓扑原因展示);grey_detail 人读详情优先,缺省回退 grey_reason 文案 */
  function inspectDisabledReason(gpu: TopologyGpu): string | null {
    const assessed = inspectGpuMap.value.get(gpu.uuid)
    if (assessed?.grey_reason) {
      return assessed.grey_detail ?? GREY_REASON_LABELS[assessed.grey_reason] ?? '不可用'
    }
    const ownerSeq = replicaIndexOfUuid.value.get(gpu.uuid)
    if (ownerSeq !== undefined && ownerSeq !== activeReplicaIndex.value + 1) {
      return `已被副本${ownerSeq}选中`
    }
    // 部署级约束(须单机语境的锁定节点)优先于份内桶约束文案
    if (singleNodeLockedNodeId.value && gpu.node_id !== singleNodeLockedNodeId.value) {
      return `${singleNodeLockReason.value}(已选 ${singleNodeLockedHostname.value})`
    }
    if (
      activeReplicaBucket.value &&
      `${gpu.node_id}|${gpu.arch}` !== activeReplicaBucket.value
    ) {
      return '须与本副本已选卡同节点同架构'
    }
    if (assessed && selectedGroupKey.value && assessed.group_key !== selectedGroupKey.value)
      return '与已选卡组不一致'
    return null
  }

  /**
   * 节点不可选原因(inspect node_support,引擎+镜像 tag 视角)。
   * 节点可选或未评估(inspect 未加载/失败/无记录)时为 null,由卡片组件回退拓扑侧原因。
   */
  function nodeDisabledReason(node: TopologyNode): string | null {
    // 单机语境锁定其它节点时,本节点整块不可选(优先于 node_support 文案)
    if (singleNodeLockedNodeId.value && node.id !== singleNodeLockedNodeId.value) {
      return `${singleNodeLockReason.value}(已选 ${singleNodeLockedHostname.value})`
    }
    const support = nodeSupportMap.value.get(node.id)
    if (!support) return null
    // cpu_lane 节点(靠内存变可选)仍展示「卡不可用」的原因,便于理解为何走 CPU
    if (support.selectable && !support.cpu_lane) return null
    return support.reason ?? '无引擎可用 GPU'
  }

  /**
   * 节点是否可作 CPU 部署落点:引擎声明 cpu(当前仅 llama.cpp)+ 节点在线。
   * 与有无可用卡无关——有卡的机器同样能跑纯 CPU 部署,节点卡片点击即选中该节点走 CPU 部署。
   */
  function isCpuNodeSelectable(node: TopologyNode): boolean {
    return engineSupportsCpu.value && node.status === 'online'
  }

  /**
   * 节点整选/取消(CPU 逐份口径):作用于激活副本——已钉本节点则仅取消本份(其它份不动),
   * 否则为本份钉定该节点。钉定即整体切到 CPU 形态:清空全部份的选卡(与点 GPU 卡清全部钉定对称)。
   * 不可选时忽略。
   */
  function toggleCpuNode(node: TopologyNode) {
    const current = cpuNodeSelections.value[activeReplicaIndex.value] ?? null
    if (current === node.id) {
      writeActiveCpuNode(null)
      return
    }
    if (!isCpuNodeSelectable(node)) return
    writeActiveCpuNode(node.id)
    // 切到 CPU 形态:GPU 选卡整体作废(cpuMode 下 derivedTopology 走 CPU 分支,残留卡会被静默忽略)
    replicaSelections.value = replicaSelections.value.map(() => [])
  }

  /** 写激活副本的 CPU 钉定值(整组换新数组,shallowRef 换引用保证响应;同步标记 touched) */
  function writeActiveCpuNode(nodeId: string | null) {
    const next = [...cpuNodeSelections.value]
    next[activeReplicaIndex.value] = nodeId
    cpuNodeSelections.value = next
    const nextTouched = [...cpuTouched.value]
    nextTouched[activeReplicaIndex.value] = true
    cpuTouched.value = nextTouched
  }

  /** 节点下可整选 GPU 列表(批量口径:手选约束叠加未被运行中部署占用,被占卡只能逐张手选) */
  function selectableGpusOf(nodeId: string): TopologyGpu[] {
    return (topology.value?.gpus ?? []).filter(
      (gpu) =>
        gpu.node_id === nodeId &&
        isGpuSelectable(gpu) &&
        !occupiedGpuUuids.value.has(gpu.uuid),
    )
  }

  /**
   * 纯收敛:候选卡收敛到单个节点(锁定节点非空时只保留之,否则取候选最多的节点,
   * 同数取拓扑顺序靠前者)。空集原样返回。
   */
  function collapseToSingleNode(candidates: Set<string>, lockedNodeId: string | null): Set<string> {
    if (!candidates.size) return candidates
    const nodeOf = new Map(
      (topology.value?.gpus ?? []).map((gpu) => [gpu.uuid, gpu.node_id] as const),
    )
    if (lockedNodeId) {
      const next = new Set<string>()
      for (const uuid of candidates) {
        if (nodeOf.get(uuid) === lockedNodeId) next.add(uuid)
      }
      return next
    }
    const byNode = new Map<string, string[]>()
    for (const uuid of candidates) {
      const nodeId = nodeOf.get(uuid) ?? ''
      const bucket = byNode.get(nodeId)
      if (bucket) bucket.push(uuid)
      else byNode.set(nodeId, [uuid])
    }
    let best: string[] = []
    for (const bucket of byNode.values()) {
      if (bucket.length > best.length) best = bucket
    }
    return new Set(best)
  }

  /**
   * 单机约束收敛:把候选卡收敛到单个节点(全选/反选/框选/推荐回填共用)。
   * 须单机语境(llama / 引擎无多机 / cann 选卡)生效;已锁定(有已选卡)时只保留锁定节点。
   * 其它语境/空集原样返回。
   */
  function narrowToSingleNode(candidates: Set<string>): Set<string> {
    if (!crossNodePpBlocked.value || candidates.size === 0) return candidates
    return collapseToSingleNode(candidates, singleNodeLockedNodeId.value)
  }

  /**
   * 须单机语境切入/残留跨节点选集时收敛(组件 watch 在语境变化时调用):
   * llama.cpp 按部署级锁把全部署对齐到同一节点(与后端 validate_cross_node 同口径);
   * 其余语境每份各自收敛(锁 = 该份点选顺序首卡节点,份间可各占不同单节点)。
   */
  function narrowSelectionToSingleNode() {
    if (!crossNodePpBlocked.value) return
    if (isLlamaCpp.value) {
      const next = replicaSelections.value.map((uuids) => [
        ...narrowToSingleNode(new Set(uuids)),
      ])
      replicaSelections.value = next
      return
    }
    const nodeOf = new Map(
      (topology.value?.gpus ?? []).map((gpu) => [gpu.uuid, gpu.node_id] as const),
    )
    const next = replicaSelections.value.map((uuids) => {
      if (!uuids.length) return uuids
      const nodes = new Set(uuids.map((uuid) => nodeOf.get(uuid) ?? ''))
      if (nodes.size <= 1) return uuids
      return [...collapseToSingleNode(new Set(uuids), nodeOf.get(uuids[0]) ?? null)]
    })
    if (next.some((uuids, index) => uuids !== replicaSelections.value[index])) {
      replicaSelections.value = next
    }
  }

  /** 新增一个空副本卡片(上限 REPLICA_COUNT_MAX)并激活它;返回是否成功 */
  function addReplica(): boolean {
    if (replicaSelections.value.length >= REPLICA_COUNT_MAX) return false
    replicaSelections.value = [...replicaSelections.value, []]
    activeReplicaIndex.value = replicaSelections.value.length - 1
    return true
  }

  /** 移除指定副本卡片(至少保留 1 份);激活指针迁移到相邻份;返回是否成功 */
  function removeReplica(index: number): boolean {
    if (replicaSelections.value.length <= 1 || index < 0 || index >= replicaSelections.value.length)
      return false
    replicaSelections.value = replicaSelections.value.filter((_, itemIndex) => itemIndex !== index)
    // CPU 钉定/动过标记按同一索引删除(尾部截断会让后续份的钉定静默左移错位)
    cpuNodeSelections.value = cpuNodeSelections.value.filter(
      (_, itemIndex) => itemIndex !== index,
    )
    cpuTouched.value = cpuTouched.value.filter((_, itemIndex) => itemIndex !== index)
    activeReplicaIndex.value = Math.min(
      Math.max(0, activeReplicaIndex.value - (activeReplicaIndex.value > index ? 1 : 0)),
      replicaSelections.value.length - 1,
    )
    return true
  }

  /** 切换激活副本(画布选卡目标随之切换) */
  function setActiveReplica(index: number) {
    if (index < 0 || index >= replicaSelections.value.length) return
    activeReplicaIndex.value = index
  }

  /** 勾选/取消单张 GPU(不可选时忽略);点卡即切到显存部署(清全部 CPU 钉定,并不再自动预选) */
  function toggleGpu(gpu: TopologyGpu) {
    if (!isGpuSelectable(gpu)) return
    if (cpuMode.value) {
      cpuNodeSelections.value = cpuNodeSelections.value.map(() => null)
      // 用户显式走显存部署:全部份标记动过,后续重检不再自动预选 CPU 落点
      cpuTouched.value = cpuTouched.value.map(() => true)
    }
    const next = new Set(selectedGpuUuids.value)
    if (next.has(gpu.uuid)) next.delete(gpu.uuid)
    else next.add(gpu.uuid)
    selectedGpuUuids.value = next
  }

  /** 节点整选:其下可选 GPU 已全部选中则全部移除,否则全部加入;非在线节点忽略 */
  function toggleNode(node: TopologyNode) {
    if (node.status !== 'online') return
    const candidates = selectableGpusOf(node.id)
    if (!candidates.length) return
    const next = new Set(selectedGpuUuids.value)
    const allSelected = candidates.every((gpu) => next.has(gpu.uuid))
    for (const gpu of candidates) {
      if (allSelected) next.delete(gpu.uuid)
      else next.add(gpu.uuid)
    }
    selectedGpuUuids.value = next
  }

  /**
   * 清空全部选择(GPU 卡 + CPU 钉定)。
   * markTouched=true(用户显式清空):全部份标记动过,后续重检不再自动回填;
   * false(程序性清空,如 applyForm 规格变化/reset):保留自动预选资格。
   */
  function clearSelection(markTouched = true) {
    replicaSelections.value = replicaSelections.value.map(() => [])
    cpuNodeSelections.value = cpuNodeSelections.value.map(() => null)
    if (markTouched) cpuTouched.value = cpuTouched.value.map(() => true)
    else cpuTouched.value = cpuTouched.value.map(() => false)
  }

  /**
   * 全选:单副本 = 全部基础可选卡入唯一份(llama.cpp 收敛单节点);
   * 多副本 = 全部基础可选卡按 (节点,架构) 桶贪心配对自动分配到各份(每份等量,
   * 桶大优先),凑不满整数份的余数留在未选中态(塞入会违反份内同桶约束,由用户手选)。
   */
  function selectAll() {
    const candidates = (topology.value?.gpus ?? []).filter((gpu) => isGpuBatchSelectable(gpu))
    const candidateUuids = new Set(candidates.map((gpu) => gpu.uuid))
    const copies = replicaSelections.value.length
    if (copies <= 1) {
      selectedGpuUuids.value = narrowToSingleNode(candidateUuids)
      return
    }
    const perCopy = Math.floor(candidates.length / copies)
    if (perCopy < 1) {
      // 可选卡不足份数:全部并入激活份,由 topologyRuleError 引导减份
      selectedGpuUuids.value = candidateUuids
      return
    }
    const buckets = new Map<string, string[]>()
    for (const gpu of candidates) {
      const key = `${gpu.node_id}|${gpu.arch}`
      const bucket = buckets.get(key)
      if (bucket) bucket.push(gpu.uuid)
      else buckets.set(key, [gpu.uuid])
    }
    const ordered = [...buckets.entries()]
      .sort(
        ([keyA, listA], [keyB, listB]) =>
          listB.length - listA.length || keyA.localeCompare(keyB),
      )
      .map(([, list]) => list)
    const next: string[][] = []
    for (let seq = 0; seq < copies; seq++) {
      const donor = ordered.find((list) => list.length >= perCopy)
      if (!donor) break
      next.push(donor.splice(0, perCopy))
    }
    // 桶凑不满的份补空(规则提示「还未选卡」);余数不并入任何份——跨桶余数强塞进某份
    // 会违反"份内同节点同架构"且难以点掉(与可选性互锁成死状态),留在未选中态由用户手选
    while (next.length < copies) next.push([])
    replicaSelections.value = next
  }

  /** 反选:激活副本在可选集合内取反(被占/离线/其它份的卡不参与;llama.cpp 收敛到单节点) */
  function invertSelection() {
    const next = new Set<string>()
    for (const gpu of topology.value?.gpus ?? []) {
      if (isGpuSelectable(gpu) && !occupiedGpuUuids.value.has(gpu.uuid) && !selectedGpuUuids.value.has(gpu.uuid))
        next.add(gpu.uuid)
    }
    selectedGpuUuids.value = narrowToSingleNode(next)
  }

  /**
   * 框选批量加入激活副本:只计入当前可选的 GPU,与已选取并集(llama.cpp 收敛到单节点)。
   * 框到 GPU 即切显存形态:清全部 CPU 钉定(形态切换确认由组件层弹窗把关)。
   */
  function addGpus(gpus: TopologyGpu[]) {
    const next = new Set(selectedGpuUuids.value)
    for (const gpu of gpus) {
      // 批量口径:框选不选入被占卡(共用显存只能逐张手选)
      if (isGpuSelectable(gpu) && !occupiedGpuUuids.value.has(gpu.uuid)) next.add(gpu.uuid)
    }
    const narrowed = narrowToSingleNode(next)
    if (!narrowed.size) return
    if (cpuMode.value) {
      cpuNodeSelections.value = cpuNodeSelections.value.map(() => null)
      cpuTouched.value = cpuTouched.value.map(() => true)
    }
    selectedGpuUuids.value = narrowed
  }

  /** 移除某节点的全部 CPU 钉定(摘要 tag 关闭时调用);被清掉的份标记动过,不再自动回填 */
  function removeCpuNodeSelection(nodeId: string) {
    let changed = false
    const next = cpuNodeSelections.value.map((pinned) => {
      if (pinned !== nodeId) return pinned
      changed = true
      return null
    })
    if (!changed) return
    cpuNodeSelections.value = next
    cpuTouched.value = cpuTouched.value.map((touched, index) =>
      next[index] === null ? true : touched,
    )
  }

  /** 移除某节点下所有已选 GPU——遍历所有份(已选摘要 tag 关闭时调用) */
  function removeNodeSelection(nodeId: string) {
    replicaSelections.value = replicaSelections.value.map((uuids) =>
      uuids.filter((uuid) => gpuByUuid.value.get(uuid)?.node_id !== nodeId),
    )
  }

  /**
   * 默认 CPU 落点:在线节点里挑「可用内存 ≥ 纯 CPU 档需求」且余量最大的那个。
   * 需求 = 逐层表 0 档 ram_gb(cpu_only 口径下已含落内存的 KV,不再单独加 kv_gb)。
   * 拿不到逐层表(模型缺形状元数据 / 预检未带 allow_cpu)→ null,不预选。
   */
  function pickDefaultCpuNode(): TopologyNode | null {
    const memory = inspect.value?.memory
    if (!memory?.tiers.length) return null
    const need = memory.tiers[0]?.ram_gb ?? 0
    const candidates = (topology.value?.nodes ?? [])
      .filter((node) => isCpuNodeSelectable(node))
      .map((node) => ({ node, avail: nodeSupportMap.value.get(node.id)?.available_memory_gb ?? 0 }))
      .filter((item) => item.avail + 1e-6 >= need)
    if (!candidates.length) return null
    candidates.sort((a, b) => b.avail - a.avail)
    return candidates[0].node
  }

  /**
   * 推荐落点回填(进入本步/规格重检后调用):
   * GPU 路径——逐份规划(replica_plans)与副本卡片数对齐时,按份序把「空份」精确回填
   * (plans[seq-1].units[].gpu_uuids),已手选的份不覆盖,用户手选优先;
   * plan 卡任一不可选(被其它份手选占用/离线/灰化/llama 锁)则该份跳过。
   * 无 plans/份数不对齐(旧后端)时回退:全部份为空才按顶层 gpu_uuids 并集填首份。
   * CPU 路径(llama.cpp cpu_only 档)——replica_plans[i].node_ids[0] 即第 i 份推荐落点,
   * 只回填「未钉且未被用户动过」的份(cpuTouched 标记),用户取消/改钉过的份不顶回;
   * 无逐份规划时全部未动份用前端启发式(pickDefaultCpuNode)钉同一节点。
   * 空落点=容量阻断不预选,由 noPlacementReason 提示。
   */
  function applyRecommendedSelection() {
    const cpuOnly = inspect.value?.memory?.scenario === 'cpu_only'
    if (cpuOnly && (isLlamaCpp.value || cpuMode.value)) {
      const placement = inspect.value?.recommended_placement
      const plans = placement?.replica_plans ?? []
      const planNodeIds = plans.map((plan) => plan.node_ids[0] ?? null)
      const nodes = topology.value?.nodes ?? []
      // 兜底候选(逐份 plans 缺失/份数不对齐时):顶层 node_ids[0](后端 binpack 权威,不做内存启发)
      // → 前端启发式(内存装得下且余量最大)
      const topId = placement?.node_ids[0] ?? null
      const topNode = topId
        ? nodes.find((item) => item.id === topId && isCpuNodeSelectable(item))
        : null
      const fallbackId = topNode?.id ?? pickDefaultCpuNode()?.id ?? null
      const aligned = plans.length === cpuNodeSelections.value.length
      const next = cpuNodeSelections.value.map((nodeId, index) => {
        // 已钉 / 用户动过的份不动(自动回填不顶回用户意图)
        if (nodeId !== null || cpuTouched.value[index]) return nodeId
        const recommendedId = aligned ? planNodeIds[index] : null
        const recommended = recommendedId
          ? nodes.find((item) => item.id === recommendedId && isCpuNodeSelectable(item))
          : null
        if (recommended) return recommended.id
        return fallbackId ?? nodeId
      })
      if (next.some((nodeId, index) => nodeId !== cpuNodeSelections.value[index])) {
        cpuNodeSelections.value = next
      }
      return
    }
    if (cpuMode.value) return
    if (isLlamaCpp.value) {
      // 非 cpu_only 档(预检解出显存档)仍默认优先 CPU:未动过的空份用前端启发式钉同一推荐节点
      // (多份同节点=原「钉定单节点」行为的逐份等价展开);用户动过/已钉的份不动。
      const fallback = pickDefaultCpuNode()
      if (fallback) {
        const next = cpuNodeSelections.value.map((nodeId, index) =>
          nodeId === null && !cpuTouched.value[index] ? fallback.id : nodeId,
        )
        if (next.some((nodeId, index) => nodeId !== cpuNodeSelections.value[index])) {
          cpuNodeSelections.value = next
        }
      }
      return
    }
    const placement = inspect.value?.recommended_placement
    if (!placement) return
    const plans = placement.replica_plans ?? []
    // 现代路径:逐份规划与卡片份数对齐,空份按份序回填(已手选的份保持不动)
    if (plans.length && plans.length === replicaSelections.value.length) {
      const next = replicaSelections.value.map((uuids, index) => {
        if (uuids.length) return uuids
        const planUuids = [...new Set(plans[index].units.flatMap((unit) => unit.gpu_uuids))]
        // 目标份为空无桶/组约束:只须批量口径(在线/未占用/未灰化/llama 锁)且未被任何份占用
        const fillable =
          planUuids.length > 0 &&
          planUuids.every((uuid) => {
            const gpu = gpuByUuid.value.get(uuid)
            // 目标份为空无桶/组约束:只须批量口径(在线/未占用/未灰化/llama 锁)且未被任何份占用
            if (!gpu) return false
            return isGpuBatchSelectable(gpu) && !replicaIndexOfUuid.value.has(uuid)
          })
        return fillable ? planUuids : uuids
      })
      if (next.some((uuids, index) => uuids !== replicaSelections.value[index])) {
        replicaSelections.value = next
      }
      return
    }
    // 回退:份数不对齐/无逐份规划,全部为空时按顶层并集填首份(兼容旧后端)
    if (allSelectedUuids.value.size) return
    const recommendedUuids = plans.length
      ? [...new Set(plans.flatMap((plan) => plan.units.flatMap((unit) => unit.gpu_uuids)))]
      : placement.gpu_uuids
    if (!recommendedUuids?.length) return
    const gpuByUuidLocal = gpuByUuid.value
    // 批量口径:推荐回填不选入被占卡(用户若要共用显存,走手选)
    const isRecommendedSelectable = (uuid: string) => {
      const gpu = gpuByUuidLocal.get(uuid)
      return Boolean(gpu) && isGpuSelectable(gpu!) && !occupiedGpuUuids.value.has(uuid)
    }
    if (!recommendedUuids.every(isRecommendedSelectable)) return
    const next = [...replicaSelections.value]
    next[0] = [...narrowToSingleNode(new Set(recommendedUuids))]
    replicaSelections.value = next
  }

  /** 清空预检缓存(模型/引擎/集群变化时调用,避免把旧评估带到下一步) */
  function clearInspect() {
    inspect.value = null
    inspectKey.value = ''
    inspectError.value = false
    editArgs.value = []
    editEnv.value = []
    resolvedArgBaseline.value = []
  }

  function addArg() {
    editArgs.value = [...editArgs.value, '']
  }

  function removeArg(index: number) {
    editArgs.value = editArgs.value.filter((_, itemIndex) => itemIndex !== index)
  }

  function addEnvVar() {
    editEnv.value = [...editEnv.value, { key: '', value: '' }]
  }

  function removeEnvVar(index: number) {
    editEnv.value = editEnv.value.filter((_, itemIndex) => itemIndex !== index)
  }

  /**
   * Step1 写回表单。
   * 集群变化:清拓扑缓存 + 预检 + 已选 GPU(防止把 A 集群的 uuid 提交给 B 集群);
   * 模型/最大上下文/副本数变化:清预检 + 已选 GPU(逐份规划与显存评估随之重算,
   * 旧选择按新份数可能凑不出整份);引擎变化:仅清预检(推荐结果随引擎变)。
   */
  function applyForm(values: WizardFormValues) {
    if (values.clusterId !== form.clusterId) {
      topology.value = null
      topologyError.value = false
      clearInspect()
      clearSelection(false)
    } else if (
      values.modelId !== form.modelId ||
      values.maxModelLen !== form.maxModelLen ||
      values.replicas !== form.replicas
    ) {
      clearInspect()
      clearSelection(false)
    } else if (values.engine !== form.engine) {
      clearInspect()
    }
    Object.assign(form, values)
  }

  /** 拓扑双写:units/copies 为权威写法,旧四数照带兼容旧读点(后端 dump 同口径) */
  function buildTopologySpec(topo: { tp: number; pp: number; replicas: number; nnodes: number; max_model_len: number | null }) {
    return {
      units: [{ role: 'full', tp: topo.tp, pp: topo.pp, ep: 1, nnodes: topo.nnodes }],
      copies: topo.replicas,
      tp: topo.tp,
      pp: topo.pp,
      ep: 1,
      replicas: topo.replicas,
      nnodes: topo.nnodes,
      max_model_len: topo.max_model_len,
    }
  }

  /**
   * Step3 提交创建:按 inspect 确认的配置 + 用户编辑的 args/env 组装 payload;写操作内部 notify → 列表自动刷新。
   * 返回创建的部署记录(嵌入方如同步完成页展示用);表单/拓扑不完整时返回 undefined。
   */
  async function submitCreate() {
    if (form.clusterId === null || form.modelId === null || !derivedTopology.value) return
    // CPU 逐份钉定未配齐不提交(正常路径被 Step2 校验拦下;防 null 混进 node_ids 或空卡集漏发)
    if (cpuMode.value && !cpuPinComplete.value) return
    // 编辑后的参数行拆回扁平 argv 作为用户层参数(三层覆盖最高层);空行剔除,全空则不传。
    // 只回传**不在预检基线里**的行:基线含系统生成项(如 --model 占位路径/--port 等),
    // 整条回传会以用户层覆盖真实值(--model 占位路径 → 容器 GGUF 打开失败)。
    const baseline = new Set(resolvedArgBaseline.value.map((line) => line.trim()))
    const extraArgs = editArgs.value
      .filter((line) => !baseline.has(line.trim()))
      .flatMap(argLineToTokens)
    const env = Object.fromEntries(
      editEnv.value.filter((item) => item.key.trim()).map((item) => [item.key.trim(), item.value]),
    )
    const engineParams: EngineParams = {}
    if (extraArgs.length) engineParams.extra_args = extraArgs
    if (Object.keys(env).length) engineParams.env = env
    const payload: CreateDeploymentPayload = {
      cluster_id: form.clusterId,
      name: form.name,
      model_id: form.modelId,
      engine: effectiveEngine.value || undefined,
      // 用户在预检 issues 弹窗确认「强制部署」后跳过后端启发式约束
      ...(forced.value ? { force: true } : {}),
      spec: {
        // topology 双写:units/copies 权威 + 旧四数派生兼容
        topology: buildTopologySpec(derivedTopology.value),
        // CPU 部署逐份钉节点(node_ids[i]=第 i 份落点,长度=副本数)+ allow_cpu + cpu_only;
        // GPU 部署按全部副本所选卡并集。cpuMode 未配齐走不到提交(Step2 校验拦),守卫兜底。
        ...(cpuMode.value && cpuPinComplete.value
          ? {
              gpu_selector: { node_ids: [...cpuNodeSelections.value] as string[] },
              allow_cpu: true,
              offload_preference: 'cpu_only' as const,
            }
          : { gpu_selector: { gpu_uuids: [...allSelectedUuids.value] } }),
        // 选了镜像版本则传 image_tag(同版本多 gpu_model 变体时由 catalog 自动裁决),不钉完整 image
        ...(form.imageTag ? { image_tag: form.imageTag } : {}),
        ...(Object.keys(engineParams).length ? { engine_params: engineParams } : {}),
        // 发布配置:部署 READY 后自动发布对外路由(对外名/定价/标签/描述)
        publish: buildPublishSpec(publish),
      },
    }
    const created = await deployStore.createDeploy(payload)
    // 暂存创建结果:提交成功后向导退出时携带,列表层据此直接打开部署详情
    lastCreated.value = created
    return created
  }

  function goToStep(step: number) {
    currentStep.value = step
  }

  /** 预检 issues 弹窗确认「强制部署」后调用,提交时 payload 带 force */
  function markForced() {
    forced.value = true
  }

  /** 向导每次打开时复位(模块单例跨次打开会残留,同 quick-setup F-001) */
  function reset() {
    currentStep.value = 1
    Object.assign(form, {
      name: '',
      modelId: null,
      engine: 'auto',
      imageTag: null,
      maxModelLen: 8 * 1024,
      clusterId: null,
      replicas: 1,
      kvCacheDtype: '',
    })
    Object.assign(publish, {
      autoPublish: true,
      name: '',
      modelType: null,
      description: '',
      tags: [],
      inputPrice: null,
      outputPrice: null,
      cachedInputPrice: null,
    })
    // 镜像目录缓存一并失效:catalog 跨次打开可能更新;引擎字典按模型缓存,复位后重拉
    imageEntries.value = []
    imageEntriesEngine.value = ''
    engines.value = []
    enginesModelId.value = null
    topology.value = null
    topologyError.value = false
    forced.value = false
    lastCreated.value = null
    // 副本卡片复位为单份空集(先重置结构再 clearSelection,避免 watch(form.replicas) 按旧长度截断)
    replicaSelections.value = [[]]
    cpuNodeSelections.value = [null]
    cpuTouched.value = [false]
    activeReplicaIndex.value = 0
    clearInspect()
    clearSelection(false)
  }

  return {
    currentStep,
    form,
    publish,
    clusterOptions,
    clusterLoading,
    engines,
    enginesLoading,
    imageEntries,
    imageEntriesLoading,
    modelOptions,
    modelLoading,
    topology,
    topologyLoading,
    topologyError,
    inspect,
    inspectLoading,
    inspectError,
    inspectGpuMap,
    nodeSupportMap,
    effectiveEngine,
    kvCacheOptions,
    kvCacheOption,
    kvCacheSelectOptions,
    hasKvCache,
    protectedFlags,
    argSuggestionOptions,
    selectedGpuUuids,
    allSelectedUuids,
    replicaSelections,
    activeReplicaIndex,
    replicaIndexOfUuid,
    selectedCpuNodeId,
    cpuNodeSelections,
    cpuPinComplete,
    activeReplicaCpuNodeId,
    occupiedGpuUuids,
    VRAM_ALLOCATABLE_RATIO,
    gpuOccupancyMap,
    editArgs,
    editEnv,
    lastCreated,
    addArg,
    removeArg,
    isArgProtected,
    addEnvVar,
    removeEnvVar,
    derivedTopology,
    topologyRuleError,
    pairingPreview,
    noPlacementReason,
    singleCopyMode,
    isLlamaCpp,
    engineSupportsMultinode,
    effectiveParallel,
    selectionIsCann,
    crossNodePpAvailable,
    crossNodePpBlocked,
    singleNodeLockedNodeId,
    singleNodeLockReason,
    crossNodeModeTip,
    engineSupportsCpu,
    engineServesModel,
    selectedModel,
    cpuMode,
    isCpuNodeSelectable,
    toggleCpuNode,
    loadClusters,
    loadEngines,
    loadImageVersions,
    engineLabel,
    engineOptionsFor,
    loadModels,
    loadTopology,
    loadInspect,
    inspectRecommendation,
    runFinalInspect,
    isGpuSelectable,
    isGpuBatchSelectable,
    inspectDisabledReason,
    nodeDisabledReason,
    selectableGpusOf,
    toggleGpu,
    toggleNode,
    clearSelection,
    selectAll,
    invertSelection,
    addGpus,
    removeNodeSelection,
    removeCpuNodeSelection,
    narrowSelectionToSingleNode,
    addReplica,
    removeReplica,
    setActiveReplica,
    applyRecommendedSelection,
    applyForm,
    submitCreate,
    goToStep,
    markForced,
    reset,
  }
}
