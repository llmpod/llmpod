import { request } from './request'

/** 大屏·集群 GPU 池行（对应后端 ScreenClusterOut） */
export interface ScreenClusterData {
  id: number
  name: string
  /** 集群下节点总数 */
  node_count: number
  /** 集群下在线节点数 */
  online_node_count: number
  /** 集群下 GPU 总数 */
  gpu_count: number
  /** GPU 核心利用率均值 0-100（在线节点按卡数加权；无实时数据为 null） */
  gpu_utilization: number | null
  /** 已用显存求和 GB（无实时数据为 null） */
  used_vram_gb: number | null
  /** 显存总量求和 GB（无实时数据为 null） */
  total_vram_gb: number | null
}

/** 大屏·部署状态计数（deleting 为瞬态不展示） */
export interface ScreenDeployCountsData {
  running: number
  creating: number
  stopped: number
  failed: number
}

/** 大屏·最近部署行 */
export interface ScreenRecentDeploymentData {
  id: string
  name: string
  /** creating | running | failed | stopped | deleting */
  status: string
}

/** 时间窗查询参数（缺省 = 今日 00:00 本地 → 当前） */
export interface DashboardWindowParams {
  /** 起始时间 ISO8601 */
  from?: string
  /** 结束时间 ISO8601 */
  to?: string
}

/** 模型统计行（GET /api/v1/admin/dashboard/models） */
export interface ModelStatData {
  /** 模型名（对外别名，可空） */
  model: string | null
  call_count: number
  /** 占比 0-1（相对全模型总调用量） */
  ratio: number
  /** 平均端到端延迟（毫秒；无样本为 null） */
  avg_latency_ms: number | null
  /** 平均首帧延迟（毫秒；仅流式，无样本为 null） */
  avg_ttft_ms: number | null
  /** 错误率 0-1 */
  error_rate: number
  /** 被输出上限截断占比 0-1（finish_reason=length|max_tokens；分母只数结束原因已知的请求，无此类行为 null） */
  truncation_rate: number | null
  tokens: number
}

/** 模型统计表（GET /api/v1/admin/dashboard/models，按调用量倒序 top N；model/tenant_id 下推 SQL） */
export function getDashboardModels(
  params?: DashboardWindowParams & { top_n?: number; model?: string; tenant_id?: number },
  signal?: AbortSignal,
): Promise<ModelStatData[]> {
  return request({ url: '/admin/dashboard/models', method: 'GET', params, signal })
}

// ==================== 三屏大屏（2026-09-15 契约 + 2026-09-17 区间化，Apifox 已逐字段核对） ====================

/** 实时大屏前端窗口词表（UI 快捷选项；2026-09-17 起后端不收 window，请求时换算 from/to） */
export type RealtimeWindow = '1h' | '24h' | 'today' | '7d' | '14d' | '30d'

/** 构成维度 */
export type RealtimeDim = 'model' | 'deployment' | 'tenant' | 'api_key'

/** 屏1 · KPI 卡（delta 量纲按卡而用；昨日无数据时 delta 全 null） */
export interface OverviewKpiData {
  /** 当前值（无样本为 null） */
  value: number | null
  /** 昨日同期 % */
  delta_pct: number | null
  /** 绝对数差（计数类） */
  delta: number | null
  /** 百分点差（比率类） */
  delta_pp: number | null
  /** 毫秒差（延迟类） */
  delta_ms: number | null
  /** 本周累计（新增租户卡） */
  week_total: number | null
}

/** 屏1 · KPI 键集 */
export type OverviewKpiKey =
  | 'calls'
  | 'active_tenants'
  | 'active_models'
  | 'success_rate'
  | 'avg_ttft_ms'
  | 'new_tenants'

/** 屏1 · 小时双线单点（hour 为窗内小时偏移，稀疏：只有有数据的小时才出现） */
export interface OverviewTrendPointData {
  hour: number
  /** 今日该小时调用量（触达上游） */
  calls: number
  /** 昨日同一小时调用量（未走到的时段为 0） */
  calls_yesterday: number
}

export interface OverviewTrendData {
  points: OverviewTrendPointData[]
  today_total: number
  yesterday_same_period_total: number
}

/** 屏1 · 上游份额卡（share 0-1，local+external 相加=1；无触达请求时 success_rate 为 null） */
export interface OverviewUpstreamCardData {
  share: number
  success_rate: number | null
}

export interface OverviewUpstreamProviderData {
  name: string
  share: number
  success_rate: number | null
}

export interface OverviewUpstreamData {
  local: OverviewUpstreamCardData
  external: OverviewUpstreamCardData
  providers: OverviewUpstreamProviderData[]
}

/** 屏1 · 服务面实时块（与指标时间窗无关） */
export interface OverviewServiceData {
  /** RUNNING 部署覆盖的去重模型数 */
  online_models: number
  /** 活跃实例数（RUNNING/READY/STARTING） */
  replicas: number
  /** 引擎 → 活跃实例数 */
  engine_mix: Record<string, number>
  deployments: ScreenDeployCountsData
  recent_deployments: ScreenRecentDeploymentData[]
  nodes: { online: number; total: number }
  gpu: { online: number; total: number }
  clusters: ScreenClusterData[]
}

/** 屏1 · 平台总览整屏（GET /admin/dashboard/overview；窗口固定=今日零点→现在，对照=昨日同时刻） */
export interface OverviewData {
  ts: string
  window: { from: string; to: string }
  kpi: Record<OverviewKpiKey, OverviewKpiData>
  trend: OverviewTrendData
  upstream: OverviewUpstreamData
  service: OverviewServiceData
}

/** 屏2/屏3 · /realtime 查询参数（2026-09-17 区间化：window/granularity 已删除，传了 422；聚合粒度由后端按跨度决定）
 *
 * 区间与对照窗约束（后端校验，422）：
 * - from >= to、跨度 > 90 天 → 422
 * - prev_from/prev_to 同给同缺；与当前区间等长（容差 1s）、终点不晚于当前区间终点、间隔 ≤ 24h
 * - 不传 prev_* 时所有 delta_* 为 null（不报错） */
export interface RealtimeParams {
  /** 区间起点 ISO8601；缺省 = 本地今日 00:00 */
  from?: string
  /** 区间终点；缺省 = 当前，晚于当前按当前处理 */
  to?: string
  /** 环比对照窗起点（与 prev_to 同给同缺） */
  prev_from?: string
  /** 环比对照窗终点（与当前区间等长，容差 1s） */
  prev_to?: string
  /** 构成维度（Literal，非法值 422） */
  dim?: RealtimeDim
  model?: string
  tenant_id?: number
  api_key_id?: number
}

/**
 * 屏2/屏3 · KPI 卡：字段按卡而用——速率卡 delta_pct、比率卡 delta_pp、
 * 延迟卡 delta_ms、计数/上下文卡 delta；in_flight 附 peak，kv_cache_util 附 instance_id。
 */
export interface RealtimeKpiData {
  value: number | null
  delta_pct: number | null
  delta_pp: number | null
  delta_ms: number | null
  delta: number | null
  /** 窗口内峰值（在途卡）；冷层为 null */
  peak: number | null
  /** 最忙实例（KV 水位卡） */
  instance_id: string | null
}

/** 屏2/屏3 · KPI 键集 */
export type RealtimeKpiKey =
  | 'qps'
  | 'tpm'
  | 'calls'
  | 'total_tokens_kpi'
  | 'avg_context'
  | 'truncation_rate'
  | 'output_tps'
  | 'in_flight'
  | 'error_rate'
  | 'p95_ms'
  | 'cache_hit_rate'
  | 'latency_avg_ms'
  | 'ttft_avg_ms'
  | 'kv_cache_util'
  | 'waiting_requests'

export interface RealtimeWindowData {
  from: string
  to: string
  /** 槽宽（5m/1h/1d，后端按区间跨度决定：≤6h→5m，≤48h→1h，>48h→1d），驱动横轴刻度 */
  granularity: string
  /** hot = 内存热层（跨度 ≤1h 且落在热环保留期）；cold = 5 分钟桶 */
  source: string
  /** 热层最后一轮抓取年龄；冷层为 null */
  age_ms: number | null
}

export interface RealtimeTrendPointData {
  /** 槽起点 ISO8601 */
  ts: string
  qpm: number
  tpm: number
  prompt_tpm: number
  completion_tpm: number
  /** 槽内请求计数（堆叠图的计数轴） */
  requests: number
  /** 槽内平均延迟；无样本为 null */
  latency_avg_ms: number | null
}

export interface RealtimeTrendData {
  /** 首槽绝对值（索引化基准）；键缺省 = 基准 ≤0，该序列不能索引 */
  baseline: Record<string, number>
  points: RealtimeTrendPointData[]
}

export interface RealtimeSeriesPointData {
  ts: string
  value: number
}

export interface RealtimeTotalPointData {
  ts: string
  tpm: number
}

/** 构成成员（__other__ = top-5 外长尾合并；tpm 与 requests 在同一槽网格上对齐） */
export interface RealtimeCompositionItemData {
  key: string
  tpm: RealtimeSeriesPointData[]
  requests: RealtimeSeriesPointData[]
}

export interface RealtimeCompositionData {
  dim: string
  total: RealtimeTotalPointData[]
  items: RealtimeCompositionItemData[]
}

/** 延迟/首字分布（累计桶，画图前必须差分） */
export interface RealtimeHistData {
  /** 13 个累计桶计数（第 i 位 = ≤ bounds[i] 的样本数，末位 = 总数） */
  buckets: number[]
  /** 桶上界（毫秒），末位 null = +Inf */
  bounds: Array<number | null>
  avg_ms: number | null
  /** 慢请求占比（latency>2500ms / ttft>1000ms） */
  slow_ratio: number | null
}

/** 输入/输出长度分布（来自 request_log 落账；窗口内无落账行或全为 0 token 时为 null） */
export interface RealtimeLenHistData {
  /** 9 个累计桶计数 */
  buckets: number[]
  /** 长度上界（token），末位 null = +Inf */
  bounds: Array<number | null>
  avg_tokens: number | null
}

export interface RealtimeDistributionData {
  latency: RealtimeHistData | null
  ttft: RealtimeHistData | null
  prompt_len: RealtimeLenHistData | null
  generation_len: RealtimeLenHistData | null
}

/** 引擎实例（ok=false 时指标字段全 null，渲染「指标不可用」而非 0） */
export interface RealtimeEngineData {
  instance_id: string
  deployment_id: string
  deployment_name: string | null
  engine: string | null
  rank: number
  /** READY/RUNNING/CRASHED/FAILED 等 */
  status: string
  ok: boolean
  kv_cache_util: number | null
  running_requests: number | null
  waiting_requests: number | null
  prefix_cache_hit_rate: number | null
  num_preemptions: number | null
  /** 快照年龄（慢档期最长为慢档间隔，照实标注） */
  age_ms: number | null
}

/** 屏2/屏3 · 实时大屏整屏（一次往返；构成整窗随响应返回，光标擦洗全在前端本地完成） */
export interface RealtimeData {
  ts: string
  window: RealtimeWindowData
  kpi: Record<RealtimeKpiKey, RealtimeKpiData>
  trend: RealtimeTrendData
  composition: RealtimeCompositionData
  distribution: RealtimeDistributionData | null
  engines: RealtimeEngineData[]
}

/** 屏2 · Token 汇总查询参数 */
export interface TokensParams {
  dim?: RealtimeDim
  from?: string
  to?: string
  top?: number
  model?: string
  tenant_id?: number
}

export interface TokenRowData {
  key: string
  input_tokens: number
  output_tokens: number
  /** 其中缓存：input 的子集，不得加进合计 */
  cached_tokens: number
  total_tokens: number
  ratio: number
}

export interface TokenOtherData extends TokenRowData {
  /** 被折叠的长尾成员数 */
  count: number
}

export interface TokenTotalData {
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  total_tokens: number
}

/** 屏2 · Token 汇总（dim 维度聚合 + 长尾折叠 + 合计行） */
export interface TokensData {
  dim: string
  from: string
  to: string
  rows: TokenRowData[]
  other: TokenOtherData | null
  total: TokenTotalData
}

/** 屏3 · KV 热力图单桶（一格可由多个 5min 桶合并而成；整格没抓到 = 缺行空档，不补 0） */
export interface EngineHistoryPointData {
  /** 桶/格起点 ISO8601 带偏移（日桶按本地零点切） */
  ts: string
  /** KV 水位格内均值 0-1（按抓取次数加权） */
  kv_avg: number | null
  /** KV 水位格内峰值 0-1 */
  kv_max: number | null
  /** 格内抢占增量之和 */
  preemptions: number
  /** 格内抓取次数之和（= 格内 5min 桶数 × 每桶抓取次数），同时是 kv_avg 的权重；0=没抓到 */
  samples: number
}

export interface EngineHistoryInstanceData {
  instance_id: string
  deployment_id: string
  deployment_name: string | null
  engine: string | null
  rank: number
  status: string
  /** 格序列（格宽随跨度 5m/1h/1d）；缺行=空档，不伪造 0；llama.cpp 恒为空数组 */
  series: EngineHistoryPointData[]
}

export interface EngineHistoryData {
  /** window.granularity = 5m/1h/1d（按区间跨度），驱动热力图列标签 */
  window: { from: string; to: string; granularity: string }
  instances: EngineHistoryInstanceData[]
}

/** 屏3 · 异常构成条目（side: rejected=准入拒绝（rollup） / error=上游错误（request_log）） */
export interface AnomalyItemData {
  /** 拒绝原因或错误粗分类（upstream_timeout/upstream_5xx/rate_limited/...） */
  kind: string
  count: number
  /** 占异常总量（错误+拒绝）0-1 */
  share: number
  side: string
}

export interface AnomalyBlockData {
  /** 错误 + 拒绝计数合计 */
  total: number
  /** 占窗口总请求比例；两数据源覆盖不一致时可能 >1，只做文本展示不做进度条 */
  ratio: number
  items: AnomalyItemData[]
}

export interface AnomalyUpstreamCardData {
  success_rate: number | null
  latency_avg_ms: number | null
  /** 窗口内每分钟触达请求数 */
  qpm: number
}

export interface AnomalyUpstreamProviderData {
  name: string
  success_rate: number | null
  latency_avg_ms: number | null
  qpm: number
}

export interface AnomalyUpstreamData {
  local: AnomalyUpstreamCardData | null
  external: AnomalyUpstreamCardData | null
  providers: AnomalyUpstreamProviderData[]
}

/** 屏3 · 异常信息 + 上游健康 */
export interface AnomalyData {
  ts: string
  window: { from: string; to: string }
  anomaly: AnomalyBlockData
  upstream: AnomalyUpstreamData
}

/** 屏1 · 平台总览整屏（GET /api/v1/admin/dashboard/overview，零参数，30s 轮询） */
export function getDashboardOverview(signal?: AbortSignal): Promise<OverviewData> {
  return request({ url: '/admin/dashboard/overview', method: 'GET', signal })
}

/** 屏2/屏3 · 实时大屏整屏（GET /api/v1/admin/dashboard/realtime；每次调用给引擎抓取续需求，轮询须 ≤10s） */
export function getDashboardRealtime(
  params?: RealtimeParams,
  signal?: AbortSignal,
): Promise<RealtimeData> {
  return request({ url: '/admin/dashboard/realtime', method: 'GET', params, signal })
}

/** 屏2 · Token 汇总表（GET /api/v1/admin/dashboard/tokens，跟随窗口/筛选变化） */
export function getDashboardTokens(
  params?: TokensParams,
  signal?: AbortSignal,
): Promise<TokensData> {
  return request({ url: '/admin/dashboard/tokens', method: 'GET', params, signal })
}

/** 屏3 · KV 热力图查询参数（2026-09-17 区间化：window 已删除；格宽随跨度 5m/1h/1d，日桶按本地零点切） */
export interface EngineHistoryParams {
  from?: string
  to?: string
}

/** 屏3 · KV 热力图（GET /api/v1/admin/dashboard/engines/history；打开即拉 + 60s）
 *  series[] 一格由多个 5min 桶合并：kv_avg 按抓取次数加权、kv_max 为格内峰值、
 *  samples 为格内抓取次数之和（= 5min 桶数 × 每桶抓取次数）、preemptions 为增量之和；整格没抓到则缺行。 */
export function getDashboardEngineHistory(
  params?: EngineHistoryParams,
  signal?: AbortSignal,
): Promise<EngineHistoryData> {
  return request({ url: '/admin/dashboard/engines/history', method: 'GET', params, signal })
}

/** 屏3 · 异常信息 + 上游健康（GET /api/v1/admin/dashboard/anomaly；30s；upstream.qpm 分母 = 区间跨度分钟数） */
export function getDashboardAnomaly(
  params?: EngineHistoryParams,
  signal?: AbortSignal,
): Promise<AnomalyData> {
  return request({ url: '/admin/dashboard/anomaly', method: 'GET', params, signal })
}
