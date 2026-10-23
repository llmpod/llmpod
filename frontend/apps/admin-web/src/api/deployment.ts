import { request } from './request'
import type { GpuStatus } from './gpu'
import type { ClusterTopology } from './topology'

/** 拓扑单元模板:每份副本内的引擎模板(v1 只放行单 full 单元,多角色留给 PD 实装) */
export interface UnitTemplate {
  /** 引擎角色;v1 仅 full */
  role: string
  tp: number
  pp: number
  ep: number
  /** 该单元跨的节点数 */
  nnodes: number
}

/** 部署拓扑(对应后端 Topology;units/copies 为权威写法,旧四数 = 单 full 单元折算派生,后端 dump 双写兼容) */
export interface DeploymentTopology {
  /** 权威写法:每份内的引擎单元模板 */
  units?: UnitTemplate[] | null
  /** 权威写法:副本份数;list = 异构权威写法(每项 = 该份的单 full 单元模板,份数 = 列表长,OAS Topology.copies) */
  copies?: number | UnitTemplate[] | null
  /** 张量并行度(派生;跨机时为全局 tp,均分到 nnodes 节点) */
  tp: number
  /** 流水线并行度(派生) */
  pp: number
  /** 专家并行度(派生) */
  ep?: number | null
  /** 副本数(派生 = copies) */
  replicas: number
  /** 每 replica 跨的节点数(>1 展开 head + headless workers;tp 须整除 nnodes) */
  nnodes: number
  /** 最大上下文长度(可空,影响 KV cache + 引擎参数) */
  max_model_len: number | null
}

/** 归一化拓扑:units/copies 缺省时按旧四数折算单 full 单元;前端展示卡数/份数统一经此取数 */
export interface NormalizedTopology {
  units: UnitTemplate[]
  copies: number
  /** 每份引擎总卡数 = 首单元 tp×pp×ep */
  gpusPerEngine: number
  maxModelLen?: number | null
}

/** 拓扑归一化(纯函数):两种写法(units×copies / 旧四数)统一为全字段有值的形状;copies 的异构 list 形态按列表长归一(OAS:份数 = 列表长) */
export function normalizedTopology(
  topology: Partial<DeploymentTopology> | null | undefined,
): NormalizedTopology {
  const units =
    topology?.units?.length
      ? topology.units
      : [
          {
            role: 'full',
            tp: topology?.tp ?? 1,
            pp: topology?.pp ?? 1,
            ep: topology?.ep ?? 1,
            nnodes: topology?.nnodes ?? 1,
          },
        ]
  const rawCopies = topology?.copies
  const copies = Array.isArray(rawCopies) ? rawCopies.length : (rawCopies ?? topology?.replicas ?? 1)
  const first = units[0]
  return {
    units,
    copies,
    gpusPerEngine: first.tp * first.pp * first.ep,
    maxModelLen: topology?.max_model_len ?? null,
  }
}

/** 手动选卡/钉节点(对应后端 GpuSelector;node_id/gpu_uuids/node_ids 三选一互斥) */
export interface GpuSelector {
  /** 指定单节点(自动分卡) */
  node_id?: string | null
  /** 手动选卡 UUID;总数=tp,跨机分布到 nnodes 节点 */
  gpu_uuids?: string[] | null
  /** 跨机选节点集;len=nnodes,每节点自动分 tp//nnodes 张 */
  node_ids?: string[] | null
}

/** 用户引擎参数(三层覆盖最高层;系统必控 flag 免疫覆盖,被忽略项经 resolved.protected_overlap 回显) */
export interface EngineParams {
  extra_args?: string[]
  env?: Record<string, string>
}

/** 引擎级系统环境(对应后端 EngineConfig) */
export interface EngineConfig {
  /** HF 镜像端点(HF_ENDPOINT) */
  hf_endpoint?: string | null
  /** 多机 NCCL 网卡名(NCCL_SOCKET_IFNAME) */
  nccl_ifname?: string | null
}

/** inspect 逐卡占用归因项(对应后端 OccupiedBy:哪个部署在某卡上声明了多少显存) */
export interface GpuOccupiedBy {
  deployment_id: string
  /** 部署名(后端缺省空串) */
  deployment_name: string
  /** 声明显存 GB */
  declared_gb: number
}

/** inspect 逐卡评估项(对应后端 cluster_gpus 元素) */
export interface InspectClusterGpu {
  uuid: string
  node_id: string
  index: number
  arch: string
  name: string
  /** 驱动/CANN 版本(catalog 区间匹配用) */
  driver_version: string | null
  total_vram_gb: number
  available_vram_gb: number
  status: GpuStatus
  /** 灰化原因:arch_mismatch(架构不被引擎支持) | no_image(引擎/镜像对该卡型号/驱动/镜像 tag 无匹配) | offline(离线);null 表示可参与选卡 */
  grey_reason: 'arch_mismatch' | 'no_image' | 'offline' | null
  /** 灰化原因人读详情(no_image 时必填,其余可空);展示时优先于 grey_reason 文案 */
  grey_detail: string | null
  /** 单卡(tp=1)即可承载;false 表示需多卡 TP */
  fit: boolean
  /** 同组才能同 TP;离线卡为 null */
  group_key: string | null
  /** 模型整体所需显存 GB(size+KV)参考;可空 */
  required_vram_gb: number | null
  /** active 部署声明占用之和 GB(status-aware:stopped/failed 不计);旧后端缺省视为 0 */
  declared_vram_gb?: number
  /** declared_vram_gb / total_vram_gb;旧后端缺省视为 0 */
  occupied_pct?: number
  /** 占用归因(哪个部署声明多少 GB);空 = 零占用;旧后端缺省视为无归因 */
  occupied_by?: GpuOccupiedBy[]
}

/** inspect 逐节点可选性(对应后端 NodeSupportInfo;引擎+镜像 tag 视角,节点选择禁用用) */
export interface NodeSupportInfo {
  node_id: string
  /** 节点主机名 */
  hostname: string
  /** 是否可作为部署目标(有 ≥1 张引擎可用卡,即 grey_reason 为 null 的在线卡) */
  selectable: boolean
  /** selectable=false 的原因(前端 tooltip 用) */
  reason: string | null
  /** 节点总内存(GB;system_buffer 实时值优先) */
  memory_gb?: number
  /** 账本口径可用内存(GB,已扣预留) */
  available_memory_gb?: number
  /** 该节点是否因 CPU 部署(内存够但无可用卡)而可选 */
  cpu_lane?: boolean
}

/** KV cache 落位与内存预算(CPU / 混合 offload;精度档仍走 engine_params.extra_args) */
export interface KVCacheConfigPayload {
  /** (已废弃,仅兼容历史 payload) */
  chunk_size?: number | null
  /** KV 真公式并发序列数(默认 4);KV 落位不可配,恒随权重 */
  n_parallel?: number | null
}

/** 逐层 offload 表的一行 */
export interface MemoryOffloadTier {
  offload_layers: number
  ram_gb: number
  vram_gb: number
  feasible: boolean
}

/** CPU / 混合 offload 资源测算(allow_cpu 且引擎支持 cpu 时返回) */
export interface MemoryEstimate {
  weight_gb: number
  /** KV 占用(GiB,已按所选精度档折算) */
  kv_gb: number
  total_layers: number
  tiers: MemoryOffloadTier[]
  scenario: 'gpu_only' | 'gpu_main' | 'cpu_main' | 'cpu_only'
  /** 推荐 offload 层数(= tiers 下标) */
  recommended_tier: number
  vram_avail_gb: number
  ram_avail_gb: number
  /** KV 口径来源:formula(真公式)| heuristic(模型缺架构元数据时的兜底) */
  kv_source: 'formula' | 'heuristic'
}

/** inspect 推荐配置 */
export interface InspectRecommendation {
  engine: string
  image: string
  topology: DeploymentTopology
  /** 推荐配置下每卡显存占用 GB */
  vram_per_gpu_gb: number
  /** 引擎声明的并行能力(tp/pp/ep 白名单;后端按请求 image_tag 版本折算,最准);缺失 → 前端回落引擎字典 */
  supported_parallel?: string[]
  /** 引擎(按请求 image_tag 版本折算)是否支持多机部署;缺失 → 前端回落引擎字典/宽松兜底 */
  supports_multinode?: boolean | null
}

/** inspect 单元规划(每份内的引擎模板落点规模) */
export interface InspectUnitPlan {
  role: string
  tp: number
  pp: number
  ep: number
  nnodes: number
  /** 每引擎总卡数 = tp×pp×ep(跨机分布在 nnodes 节点,每节点 ÷nnodes) */
  gpus_per_engine: number
  /** 该单元解析镜像 */
  image: string | null
}

/** inspect 单元落点(一份副本内某引擎单元的卡分配) */
export interface InspectUnitPlacement {
  role: string
  gpu_uuids: string[]
  /** 每张卡 available − 每卡所需 ≥ 0 */
  fit: boolean
  /** min(available) − required GB;不 fit 为 null */
  headroom_gb: number | null
}

/** inspect 逐份规划(手选/自动配对结果;异构部署时各份 arch 可能不同) */
export interface InspectReplicaPlan {
  /** 副本序号(人读从 1 起) */
  seq: number
  node_ids: string[]
  arch: string
  units: InspectUnitPlacement[]
  fit: boolean
  /** 配不齐/不 fit 的人读原因 */
  issues: string[]
}

/** inspect 后端推荐选卡(与部署期同一套调度内核;null 表示无 active 集群/无模型,空 gpu_uuids 表示无满足组合) */
export interface InspectRecommendedPlacement {
  /** 推荐选卡(单节点 = tp×pp 张;跨机合计同数) */
  gpu_uuids: string[]
  /** 所跨节点(单节点 1 个;跨机 nnodes 个) */
  node_ids: string[]
  /** 所选架构(adapter 默认) */
  arch: string
  /** 推荐总卡数 = gpus_per_engine × copies */
  total_gpus?: number
  /** arch → 卡数 */
  arch_summary?: Record<string, number>
  unit_plans?: InspectUnitPlan[]
  replica_plans?: InspectReplicaPlan[]
}

/** 三层解析回显(传了 engine+topology 才返回;仅 model_id 阶段为 null) */
export interface InspectResolved {
  image: string
  /** 最终命令行参数预览 */
  args: string[]
  env: Record<string, string>
  /** 用户 extra_args 撞系统必控项(已被忽略) */
  protected_overlap: string[]
}

/** 部署预检响应(POST /api/v1/admin/model-deployments/inspect) */
export interface InspectDeploymentResult {
  /** 以该字段为准;issues 非空时仍可能为 true(纯告警) */
  deployable: boolean
  /** 校验/告警文案,直接展示 */
  issues: string[]
  recommended_cluster_id: number | null
  /** true 提示「将跨机部署」 */
  cross_node: boolean
  /** 推荐配置(OpenAPI 必填;仅 model_id 阶段也返回 adapter 默认推荐) */
  recommendation: InspectRecommendation
  cluster_gpus: InspectClusterGpu[]
  /** 逐节点可选性(引擎+镜像 tag 视角);无集群/无在线节点时为空 */
  node_support: NodeSupportInfo[]
  /** 后端推荐选卡;三态:null 不展示 / gpu_uuids 为空提示无可调度卡 / 非空直接预选 */
  recommended_placement: InspectRecommendedPlacement | null
  resolved: InspectResolved | null
  /** CPU / 混合 offload 资源测算(allow_cpu 且引擎支持 cpu 时非空) */
  memory?: MemoryEstimate | null
  /** 推荐集群拓扑预览;Step3 从 engines 取 protected_flags/param_suggestions 驱动参数编辑 */
  preview_topology?: ClusterTopology | null
}

/** 部署预检请求体:三阶段渐进,按请求完整度自动推进 */
export interface InspectDeploymentPayload {
  /** 模型 ID(必填) */
  model_id: number
  /** 集群 ID;不填则遍历 active 集群推荐 */
  cluster_id?: number | null
  /** 引擎;不填则推荐(vllm|sglang|llamacpp) */
  engine?: string | null
  /** 拓扑;不填则推荐 */
  topology?: Partial<DeploymentTopology> | null
  /** 手动选卡;不填自动调度 */
  gpu_selector?: GpuSelector | null
  engine_params?: EngineParams
  engine_config?: EngineConfig
  /** 镜像全量覆盖(escape-hatch) */
  image?: string | null
  /** 镜像 tag(= catalog engine_version 维度) */
  image_tag?: string | null
  /** 模型权重路径覆盖(一般不传,走自动下发) */
  model_path?: string | null
  /** 允许调度落到 CPU / 混合 offload(手选 GPU 卡时后端忽略该部署) */
  allow_cpu?: boolean
  /** 部署偏好:auto(默认)| gpu_only | gpu_main | cpu_main | cpu_only */
  offload_preference?: string
  /** KV cache 落位与内存预算 */
  kv_cache?: KVCacheConfigPayload
}

/**
 * 部署自动发布配置(对应后端 spec.publish;部署 READY 后由 _finalize 钩子消费)。
 * 只决定对外服务名(Route.name,租户调用 model= 用的名字);引擎内部
 * --served-model-name / 端点 served_model 仍用 model.name,数据面已解耦,前端无感。
 */
export interface PublishConfig {
  /** 部署达成后是否自动发布对外路由;false 只部署不发布(端点仍建,路由/绑定不建) */
  auto_publish?: boolean
  /** 对外服务名(租户调用 model= 字段);null → model.name;strip 后不得为空白 */
  name?: string | null
  /** 模型类别;null → model.model_type */
  model_type?: string | null
  /** 模型描述(模型中心展示,可空) */
  description?: string | null
  /** 能力标签(受控词表 ModelTag,未知标签 422;纯展示) */
  tags?: string[]
  /** 输入定价(每百万 token / 元;null=不计费) */
  input_price_per_mtok?: number | null
  /** 输出定价(每百万 token / 元;null=不计费) */
  output_price_per_mtok?: number | null
  /** 缓存命中输入定价(每百万 token / 元;null=不计费) */
  cached_input_price_per_mtok?: number | null
}

/** 创建部署 spec(对应后端 DeploymentConfig) */
export interface DeploymentConfig {
  topology?: Partial<DeploymentTopology>
  engine_params?: EngineParams
  health_check?: { timeout_seconds?: number; interval_seconds?: number }
  engine_config?: EngineConfig
  gpu_selector?: GpuSelector | null
  image?: string | null
  image_tag?: string | null
  model_path?: string | null
  /** 允许调度落到 CPU / 混合 offload(与手选 GPU 卡互斥) */
  allow_cpu?: boolean
  /** 部署偏好:auto(默认)| gpu_only | gpu_main | cpu_main | cpu_only */
  offload_preference?: string
  /** KV cache 落位与内存预算 */
  kv_cache?: KVCacheConfigPayload
  /** 部署自动发布配置(对外名/定价/标签/描述;不传走后端默认 auto_publish=true) */
  publish?: PublishConfig
}

/** 创建部署请求体(POST /api/v1/admin/model-deployments) */
export interface CreateDeploymentPayload {
  /** 集群 ID(用 inspect 的 recommended_cluster_id) */
  cluster_id: number
  name: string
  model_id: number
  /** 默认 vllm;vllm|sglang|llamacpp */
  engine?: string
  spec?: DeploymentConfig
  /** true 跳过预检/同子网等启发式约束 */
  force?: boolean
}

/** 部署状态(长期状态,前端主看这个) */
export type DeploymentStatus = 'creating' | 'running' | 'failed' | 'stopped' | 'deleting'

/** 创建部署响应 data(对应后端 ModelDeploymentOut) */
export interface ModelDeploymentOut {
  /** 部署 ID(轮询/详情用) */
  deployment_id: string
  cluster_id: number
  name: string
  model_id: number
  engine: string
  status: DeploymentStatus
  /** 部署完成后才有值 */
  api_endpoint: string | null
  last_error: string | null
  /** 编排子态(仅 creating 期推进,用于进度条) */
  phase: string
  version: number
  spec: DeploymentConfig
  created_at: string
  updated_at: string
}

/** 部署预检(POST /api/v1/admin/model-deployments/inspect):推荐配置 + 校验 + 逐卡评估 */
export function inspectDeployment(data: InspectDeploymentPayload): Promise<InspectDeploymentResult> {
  return request({ url: '/admin/model-deployments/inspect', method: 'POST', data })
}

/** 创建部署(POST /api/v1/admin/model-deployments):落库 status=creating 并唤醒控制循环,立即返回;结果靠轮询 */
export function createDeployment(data: CreateDeploymentPayload): Promise<ModelDeploymentOut> {
  return request({ url: '/admin/model-deployments', method: 'POST', data })
}

/** 编排子态(creating 期推进;STOPPING 停止时插入;SCALING 在线扩缩容时插入,status 仍 running) */
export type DeploymentPhase =
  | 'PENDING'
  | 'STAGING_MODEL'
  | 'PULL_IMAGE'
  | 'START_HEAD'
  | 'WAIT_HEAD_READY'
  | 'START_WORKERS'
  | 'WAIT_WORKERS_READY'
  | 'FINALIZING'
  | 'RUNNING'
  | 'STOPPING'
  | 'SCALING'

/** 实例状态(对应后端 Instance.status;CRASHED = 容器崩溃后端会重试) */
export type InstanceStatus =
  | 'PENDING'
  | 'STARTING'
  | 'RUNNING'
  | 'READY'
  | 'CRASHED'
  | 'FAILED'
  | 'STOPPING'
  | 'STOPPED'

/** 部署实例(对应后端 InstanceSchema;仅声明前端展示所需字段) */
export interface DeploymentInstance {
  instance_id: string
  deployment_id: string
  /** 角色:full | prefill | decode | ... */
  role: string
  /** 实例全局序号(head=0,worker>0) */
  rank: number
  /** 分配的节点 ID(调度后填) */
  assigned_node: string | null
  status: InstanceStatus
  container_id: string | null
  /** 实例服务端点(Ready 后回填,如 http://ip:port) */
  endpoint: string | null
  /** 最近一次失败原因 */
  last_error: string | null
  started_at: string | null
  ready_at: string | null
  stopped_at: string | null
}

/** 推理端点(对应后端 ModelEndpointSchema;仅声明前端展示所需字段) */
export interface ModelEndpoint {
  /** 端点 ID(副本↔端点映射 join 用) */
  id?: number
  /** 端点名;多副本为「部署名·r{seq}」(人读从 1 起),单副本裸名 */
  name?: string | null
  engine_type: string
  /** 协议:openai | ... */
  protocol: string
  /** API 基址,如 http://10.0.1.5:8000 */
  api_base: string
  /** 主服务端点 path(按类别派生,如 /v1/chat/completions) */
  path: string | null
  /** served 模型名(client 请求里的 model 字段) */
  served_model: string
  context_window: number | null
  operations: string[]
  /** 健康状态:healthy | degraded | down */
  health_status: string
}

/** 引擎实例行(详情 replicas[].engines;投影链权威状态);OAS 中 shard_ids 非必填 */
export interface DeploymentEngineInstance {
  instance_id: string
  role: string
  tp: number
  pp: number
  ep: number
  nnodes: number
  gpu_arch: string | null
  /** provisioning | starting | ready | down */
  status: 'provisioning' | 'starting' | 'ready' | 'down'
  /** 容器分片引用 → detail.instances[].instance_id;OAS 非必填,消费方需判空 */
  shard_ids?: string[]
}

/** 副本行(详情 replicas;每副本一条端点);OAS required 仅 [replica_id, seq, status],engines/endpoint_ids 非必填 */
export interface DeploymentReplica {
  replica_id: string
  /** 副本序号;OAS 为 0 基(份内序号 0..copies-1,缩容 LIFO 从最大 seq 删),展示统一经 toReplicaSeq 转 1 基 */
  seq: number
  /** provisioning | serving | down */
  status: 'provisioning' | 'serving' | 'down'
  /** 份内引擎实例;OAS 非必填 */
  engines?: DeploymentEngineInstance[]
  /** 端点 ID 列表 → endpoints[].id;OAS 非必填 */
  endpoint_ids?: number[]
}

/**
 * 副本序号 0 基 → 人读 1 基(全站统一收口:后端 seq 为 0..copies-1,展示为「副本N」从 1 起;
 * 推导路径(缺 replicas 层)按 index+1 生成,两条路径经此函数对齐)。
 */
export function toReplicaSeq(seq: number): number {
  return seq + 1
}

/** 部署详情(GET /api/v1/admin/model-deployments/{deployment_id}) */
export interface DeploymentDetailOut {
  deployment: ModelDeploymentOut
  instances: DeploymentInstance[]
  /** 推理端点列表(多副本时每副本一条;部署中为空数组) */
  endpoints: ModelEndpoint[]
  /** 副本层组图数据;缺失时前端回退 instances 均分启发式 */
  replicas?: DeploymentReplica[]
}

/** 列出部署查询参数 */
export interface ListDeploymentsParams {
  cluster_id?: number | null
  status?: string | null
  /** 页码,从 1 开始 */
  page?: number
  page_size?: number
}

/** 部署分页数据(GET /api/v1/admin/model-deployments) */
export interface DeploymentPageData {
  items: ModelDeploymentOut[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 列出部署(GET /api/v1/admin/model-deployments):分页 + cluster_id/status 过滤,前端据此轮询状态 */
export function listDeployments(
  params: ListDeploymentsParams,
  signal?: AbortSignal,
): Promise<DeploymentPageData> {
  return request({ url: '/admin/model-deployments', method: 'GET', params, signal })
}

/** 部署详情(GET /api/v1/admin/model-deployments/{deployment_id}):deployment + instances + endpoint */
export function getDeployment(deploymentId: string): Promise<DeploymentDetailOut> {
  return request({ url: `/admin/model-deployments/${deploymentId}`, method: 'GET' })
}

/** 实例启动命令(GET .../instances/{instance_id}/command;对应后端 InstanceCommandOut) */
export interface InstanceCommand {
  /** 是否取到启动命令(无 START 任务行/下发窗口期为 false,非错误) */
  found: boolean
  /** 完整 docker run 命令(下发那一刻的 DeploySpec 快照渲染,含续行符可直接复制) */
  command: string | null
  /** 容器镜像 */
  image: string | null
  /** 容器名 */
  container_name: string | null
  /** 下发任务状态(SUCCESS/FAILED/...) */
  task_status: string | null
  /** 尝试序号 */
  attempt: number | null
  /** 下发时间(ISO8601) */
  dispatched_at: string | null
}

/** 实例启动命令(GET /admin/model-deployments/{id}/instances/{iid}/command):该容器实际下发的完整 docker run */
export function getInstanceCommand(
  deploymentId: string,
  instanceId: string,
): Promise<InstanceCommand> {
  return request({
    url: `/admin/model-deployments/${deploymentId}/instances/${instanceId}/command`,
    method: 'GET',
  })
}

/** 部署每节点步骤摘要项(GET .../logs/nodes;对应后端 DeployNodeSummaryItem,字段均可空) */
export interface DeployNodeSummaryItem {
  /** 节点 ID */
  node_id: string | null
  /** 节点主机名(可读名,前端优先展示) */
  node_hostname: string | null
  /** 节点 IP */
  node_ip: string | null
  /** 当前步骤(词表见 DeployLogItem.step;最后一条带 node_id 的日志) */
  step: string | null
  /** 步骤进度状态(pending/running/success/failed/cancelled,≠ 部署 status) */
  status: string | null
  /** 日志级别 */
  level: string | null
  /** 最后一条消息 */
  message: string | null
  /** 最后一条日志时间(ISO8601) */
  updated_at: string | null
}

/** 部署每节点当前步骤/状态(GET /admin/model-deployments/{id}/logs/nodes):拓扑节点徽标数据 */
export function getDeploymentNodeSummary(deploymentId: string): Promise<DeployNodeSummaryItem[]> {
  return request({ url: `/admin/model-deployments/${deploymentId}/logs/nodes`, method: 'GET' })
}

/** 部署全流程日志项(GET /{id}/logs;对应后端 DeployLogSchema,created_at 升序) */
export interface DeployLogItem {
  /** 日志 ID(自增;增量按行去重用) */
  id: number
  deployment_id: string
  /** 节点 ID(编排事件为空;实例生命周期事件有值) */
  node_id: string | null
  task_id: string | null
  /** 编排:queued/staging_model/pull_image/start_head/head_health_check/start_workers/worker_health_check/finalize/running;事件:submit/start/stop/stopping/start_instance/orchestration/scale;wait_ready 已废弃(历史数据兜底) */
  step: string
  /** debug | info | warn | error */
  level: string
  /** 步骤进度状态快照(pending/running/success/failed/cancelled);编排事件为 null */
  status: string | null
  message: string | null
  /** 结构化扩展(container_id/instance_id/endpoint 等) */
  extra: Record<string, unknown> | null
  created_at: string
}

/** 时间线查询参数(全可选;过滤态下增量轮询需原样带上) */
export interface ListDeploymentLogsParams {
  node_id?: string | null
  task_id?: string | null
  step?: string | null
  level?: string | null
  status?: string | null
  /** 起始时间(含,ISO8601);增量轮询传本地最后一条 created_at */
  since?: string | null
  until?: string | null
  /** message 关键字(大小写不敏感包含) */
  search?: string | null
  page?: number
  /** 上限 500 */
  page_size?: number
}

/** 部署日志分页数据(PaginatedResponse_DeployLogSchema_ 的 data) */
export interface DeployLogPageData {
  items: DeployLogItem[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 部署全流程日志(GET /admin/model-deployments/{id}/logs):结构化时间线,升序分页 + since 增量 */
export function listDeploymentLogs(
  deploymentId: string,
  params?: ListDeploymentLogsParams,
  signal?: AbortSignal,
): Promise<DeployLogPageData> {
  return request({
    url: `/admin/model-deployments/${deploymentId}/logs`,
    method: 'GET',
    params,
    signal,
  })
}

/** 停止部署(POST .../stop):可停状态 running/failed/creating,返回 { deployment_id, status: 'stopped' } */
export function stopDeployment(
  deploymentId: string,
): Promise<{ deployment_id: string; status: DeploymentStatus }> {
  return request({ url: `/admin/model-deployments/${deploymentId}/stop`, method: 'POST' })
}

/**
 * 启动(重启)部署(POST .../start):仅 stopped/failed 可启动,原卡重新预留 VRAM 后重部署;
 * 原卡被其他部署占用返回 409(data.occupied 明细),force=true 强制抢占(自负 OOM 风险)。
 * silentError:409 占用需前端二次确认强制启动,错误提示由调用方统一处理。
 */
export function startDeployment(
  deploymentId: string,
  force = false,
): Promise<Record<string, unknown>> {
  return request({
    url: `/admin/model-deployments/${deploymentId}/start`,
    method: 'POST',
    params: force ? { force: true } : undefined,
    meta: { silentError: true },
  })
}

/** 删除部署(POST .../delete):标 deleting → 停容器 → 级联删实例/端点/部署,返回 { deployment_id, deleted: true } */
export function deleteDeployment(
  deploymentId: string,
): Promise<{ deployment_id: string; deleted: boolean }> {
  return request({ url: `/admin/model-deployments/${deploymentId}/delete`, method: 'POST' })
}

/** 在线扩缩预览(POST .../scale/preview):只读,不落库 */
export interface ScalePreviewOut {
  /** 当前份数 */
  current_copies: number
  /** 每份卡数(单副本规格不可变) */
  per_copy_gpus: number
  /** 集群空闲卡可支撑的最大份数(含存量) */
  max_feasible_copies: number
  /** 集群空闲卡数 */
  free_gpus: number
  /** 扩容到 max_feasible_copies 的建议落点;差卡为 null */
  suggested_placement: { gpu_uuids: string[]; node_ids: string[] } | null
}

/** 扩缩预览(POST /admin/model-deployments/{id}/scale/preview):可扩上限/空闲卡/建议落点 */
export function scalePreview(deploymentId: string): Promise<ScalePreviewOut> {
  return request({ url: `/admin/model-deployments/${deploymentId}/scale/preview`, method: 'POST' })
}

/** 在线扩缩(POST /admin/model-deployments/{id}/scale):即时受理,进度靠轮询(phase 转入 SCALING);
 *  仅 running 可调;SCALING 中或目标=当前返回 409(错误提示走 HTTP 层默认行为) */
export function scaleDeployment(
  deploymentId: string,
  copies: number,
): Promise<{ deployment_id: string; copies: number; status: string }> {
  return request({
    url: `/admin/model-deployments/${deploymentId}/scale`,
    method: 'POST',
    data: { copies },
  })
}
