import { ref } from 'vue'
import {
  getDashboardOverview,
  type OverviewData,
  type OverviewKpiKey,
  type ScreenClusterData,
  type ScreenRecentDeploymentData,
} from '@/api/dashboard'
import {
  buildKpiCard,
  EMPTY_TEXT,
  formatCompact,
  formatLatency,
  formatRate,
  type KpiCard,
  type KpiCardSpec,
  padHourTrend,
  trimZero,
} from '@/views/dashboard-shared/format'

/** 调用趋势双线视图模型(0-23 小时连续铺,缺小时补 0) */
export interface TrendView {
  labels: string[]
  today: number[]
  yesterday: number[]
}

/** 上游构成条目(local/external/供应商共用) */
export interface UpstreamItem {
  name: string
  /** 占比文案(81.2%) */
  shareText: string
  /** 进度条宽度 0-100 */
  sharePct: number
  successText: string
  /** 成功率低于阈值 → 标「需关注」 */
  attention: boolean
}

/** 健康横幅(前端自算,§8 #2:后端无健康端点) */
export interface BannerView {
  ok: boolean
  items: string[]
}

/** 节点/GPU 在线规模(percent 与 UpstreamItem.sharePct 同口径) */
export interface OnlineInfo {
  online: number
  total: number
  ok: boolean
  /** 在线率 0-100;total=0 视为 100 */
  percent: number
}

/** 引擎分布条目(保持后端返回顺序,避免轮询间行序跳动) */
export interface EngineMixItem {
  engine: string
  count: number
}

/** 服务面规模行 */
export interface ServiceInfo {
  onlineModels: number
  replicas: number
  engineMix: EngineMixItem[]
}

/** 部署概览汇总项(点击跳转部署列表按状态筛选) */
export interface DeploySummaryItem {
  label: string
  value: number
  tone: 'success' | 'warning' | 'error' | 'default'
  status: string
}

/** 部署概览行 */
export interface DeployItem {
  id: string
  name: string
  status: string
  statusLabel: string
  statusType: 'success' | 'warning' | 'error' | 'default'
}

/** GPU 集群池 */
export interface GpuPool {
  id: number
  name: string
  gpuCount: number
  /** 利用率 0-100;null = 无实时数据 */
  percent: number | null
  meta: string
  /** 利用率 ≥80 高载,进度条转警示色 */
  hot: boolean
}

/** 部署状态 → 标签/配色(对齐后端状态机;未知值原样展示) */
const DEPLOY_STATUS_MAP: Record<string, { label: string; type: DeployItem['statusType'] }> = {
  running: { label: '运行中', type: 'success' },
  creating: { label: '部署中', type: 'warning' },
  stopped: { label: '已停止', type: 'default' },
  failed: { label: '失败', type: 'error' },
  deleting: { label: '删除中', type: 'default' },
}

/** 上游需关注阈值(§8 #9:阈值告警后端未做,前端先定) */
export const UPSTREAM_ATTENTION_RATE = 0.9

/** KPI 六卡配置(§2.2:每张各用一种 delta 量纲) */
const KPI_SPECS: Array<KpiCardSpec & { key: OverviewKpiKey }> = [
  {
    key: 'calls',
    label: '调用量 · 今日',
    kind: 'pct',
    badDirection: 'down',
    format: value => formatCompact(value),
  },
  {
    key: 'active_tenants',
    label: '活跃租户',
    kind: 'abs',
    badDirection: 'down',
    format: value => String(value),
  },
  {
    key: 'active_models',
    label: '活跃模型',
    kind: 'abs',
    badDirection: 'down',
    format: value => String(value),
  },
  {
    key: 'new_tenants',
    label: '新增租户',
    kind: null,
    badDirection: 'down',
    format: value => String(value),
    note: raw => (raw.week_total == null ? null : `本周累计 ${raw.week_total} 家`),
  },
  {
    key: 'success_rate',
    label: '成功率 · 今日',
    kind: 'pp',
    badDirection: 'down',
    format: value => formatRate(value),
  },
  {
    key: 'avg_ttft_ms',
    label: '平均首帧 · 今日',
    kind: 'ms',
    badDirection: 'up',
    format: value => formatLatency(value),
  },
]

function toUpstreamItem(name: string, item: { share: number; success_rate: number | null }): UpstreamItem {
  return {
    name,
    shareText: formatRate(item.share, 1),
    sharePct: Math.round(item.share * 1000) / 10,
    successText: item.success_rate === null ? EMPTY_TEXT : formatRate(item.success_rate),
    attention: item.success_rate !== null && item.success_rate < UPSTREAM_ATTENTION_RATE,
  }
}

/** 健康横幅自算(§8 #2:节点/GPU 在线率 + 失败部署 + 外部上游成功率) */
function toBanner(data: OverviewData): BannerView {
  const items: string[] = []
  if (data.service.nodes.total > data.service.nodes.online)
    items.push(`节点 ${data.service.nodes.online}/${data.service.nodes.total} 在线`)
  if (data.service.gpu.total > data.service.gpu.online)
    items.push(`GPU ${data.service.gpu.online}/${data.service.gpu.total} 在线`)
  if (data.service.deployments.failed > 0)
    items.push(`${data.service.deployments.failed} 个部署失败`)
  const external = data.upstream.external
  if (external.success_rate !== null && external.success_rate < UPSTREAM_ATTENTION_RATE)
    items.push(`外部上游成功率 ${formatRate(external.success_rate)}`)
  return { ok: items.length === 0, items }
}

/** 节点/GPU 在线规模:在线率四舍五入取整,total=0 视为 100% */
function toOnlineInfo(online: number, total: number): OnlineInfo {
  return {
    online,
    total,
    ok: total === 0 || online === total,
    percent: total === 0 ? 100 : Math.round((online / total) * 100),
  }
}

function toGpuPools(clusters: ScreenClusterData[]): GpuPool[] {
  return clusters.map((cluster) => {
    return {
      id: cluster.id,
      name: cluster.name,
      gpuCount: cluster.gpu_count,
      percent: cluster.gpu_utilization,
      meta: `${cluster.online_node_count}/${cluster.node_count} 节点 · 显存 ${
        cluster.used_vram_gb !== null && cluster.total_vram_gb !== null
          ? `${trimZero(cluster.used_vram_gb.toFixed(1))}/${trimZero(cluster.total_vram_gb.toFixed(1))}GB`
          : '—'
      }`,
      hot: (cluster.gpu_utilization ?? 0) >= 80,
    }
  })
}

function toDeployList(rows: ScreenRecentDeploymentData[]): DeployItem[] {
  return rows.map((row) => {
    const conf = DEPLOY_STATUS_MAP[row.status] ?? { label: row.status, type: 'default' as const }
    return {
      id: row.id,
      name: row.name,
      status: row.status,
      statusLabel: conf.label,
      statusType: conf.type,
    }
  })
}

function toEngineMix(mix: Record<string, number>): EngineMixItem[] {
  return Object.entries(mix).map(([engine, count]) => ({ engine, count }))
}

// ---- 模块级状态(单例;沿用现有两屏写法) ----

const kpiCards = ref<KpiCard[]>([])
const trendView = ref<TrendView>({ labels: [], today: [], yesterday: [] })
const trendTotalsText = ref('')
const upstreamCards = ref<{ local: UpstreamItem; external: UpstreamItem; providers: UpstreamItem[] } | null>(null)
const banner = ref<BannerView>({ ok: true, items: [] })
const serviceInfo = ref<ServiceInfo>({ onlineModels: 0, replicas: 0, engineMix: [] })
const nodesInfo = ref<OnlineInfo>({ online: 0, total: 0, ok: true, percent: 100 })
const gpuInfo = ref<OnlineInfo>({ online: 0, total: 0, ok: true, percent: 100 })
const gpuPools = ref<GpuPool[]>([])
const deploySummary = ref<DeploySummaryItem[]>([])
const deployList = ref<DeployItem[]>([])

/**
 * 屏1 数据 store:/admin/dashboard/overview 整屏聚合。
 * 窗口固定=今日零点→现在,对照=昨日同时刻(§2);30s 轮询由 controller 驱动。
 */
export function useStore() {
  const fetchOverview = async (signal?: AbortSignal) => {
    const data = await getDashboardOverview(signal)

    kpiCards.value = KPI_SPECS.map(spec => buildKpiCard(spec, data.kpi[spec.key], ' vs 昨日'))

    const padded = padHourTrend(data.trend.points)
    const pad2 = (n: number) => String(n).padStart(2, '0')
    trendView.value = {
      labels: padded.map(point => `${pad2(point.hour)}:00`),
      today: padded.map(point => point.calls),
      yesterday: padded.map(point => point.calls_yesterday),
    }
    trendTotalsText.value = `今日 ${formatCompact(data.trend.today_total)} · 昨日同期 ${formatCompact(data.trend.yesterday_same_period_total)}`

    upstreamCards.value = {
      local: toUpstreamItem('本地推理', data.upstream.local),
      external: toUpstreamItem('外部上游', data.upstream.external),
      providers: data.upstream.providers.map(provider => toUpstreamItem(provider.name, provider)),
    }

    banner.value = toBanner(data)

    serviceInfo.value = {
      onlineModels: data.service.online_models,
      replicas: data.service.replicas,
      engineMix: toEngineMix(data.service.engine_mix),
    }
    nodesInfo.value = toOnlineInfo(data.service.nodes.online, data.service.nodes.total)
    gpuInfo.value = toOnlineInfo(data.service.gpu.online, data.service.gpu.total)
    gpuPools.value = toGpuPools(data.service.clusters)

    deploySummary.value = [
      { label: '运行中', value: data.service.deployments.running, tone: 'success', status: 'running' },
      { label: '部署中', value: data.service.deployments.creating, tone: 'warning', status: 'creating' },
      { label: '已停止', value: data.service.deployments.stopped, tone: 'default', status: 'stopped' },
      { label: '失败', value: data.service.deployments.failed, tone: 'error', status: 'failed' },
    ]
    deployList.value = toDeployList(data.service.recent_deployments)
  }

  return {
    kpiCards,
    trendView,
    trendTotalsText,
    upstreamCards,
    banner,
    serviceInfo,
    nodesInfo,
    gpuInfo,
    gpuPools,
    deploySummary,
    deployList,
    fetchOverview,
  }
}
