import type { HttpRequestMeta } from '@repo/http-client'
import { request } from './request'

/** 节点状态：online=在线 | offline=离线 | maintenance=维护 | busy=繁忙 | drained=已排空 */
export type NodeStatus = 'online' | 'offline' | 'maintenance' | 'busy' | 'drained'

/** 节点版本状态（比对基准是 manager 版本，后端已比较，前端只管渲染四态）：ok=同版 | outdated=落后 | newer=超前 | unknown=未上报 */
export type NodeVersionStatus = 'ok' | 'outdated' | 'newer' | 'unknown'

/** 节点实时资源使用摘要（对应后端 NodeRuntimeOut；数据来自 Manager 心跳缓冲，非 DB） */
export interface NodeRuntime {
  /** CPU 使用率 0-100 */
  cpu_percent: number
  /** 已用内存 GB（总量读节点静态字段 memory_gb） */
  memory_used_gb: number
  /** 内存使用率 0-100 */
  memory_percent: number
  /** 根分区总量 GB */
  disk_total_gb: number
  /** 根分区已用 GB */
  disk_used_gb: number
  /** 根分区使用率 0-100 */
  disk_percent: number
  /** GPU 数量 */
  gpu_count: number
  /** 全部 GPU 核心利用率均值 0-100 */
  gpu_utilization: number
  /** 全部 GPU 已用显存求和 GB */
  used_vram_gb: number
  /** 全部 GPU 显存总量求和 GB */
  total_vram_gb: number
}

/** 节点信息（对应后端 NodeDetailOut） */
export interface NodeData {
  /** 节点ID（Manager 分配的 UUID） */
  id: string
  /** 所属集群ID */
  cluster_id: number
  /** 节点主机名 */
  hostname: string
  /** 节点 IP 地址 */
  ip: string
  /** 备注（可空） */
  remark: string | null
  status: NodeStatus
  /** 最近一次心跳时间（可空） */
  last_heartbeat_at: string | null
  /** CPU 核数（可空） */
  cpu_cores: number | null
  /** CPU 架构，如 x86_64 / arm64（可空） */
  cpu_arch: string | null
  /** 内存总量 GB（可空） */
  memory_gb: number | null
  created_at: string
  updated_at: string
  /** 所属集群名（派生字段，无关联时为 null） */
  cluster_name: string | null
  /** 节点运行的 llmpod 版本（null = 未上报/存量节点） */
  version: string | null
  /** 版本状态（比对基准是 manager 版本，前端不比较，直接渲染） */
  version_status: NodeVersionStatus
  /** 实时资源使用摘要（在线节点为对象，离线节点为 null，勿当 0 用） */
  runtime: NodeRuntime | null
}

/** 节点分页数据（GET /api/v1/admin/nodes） */
export interface NodePageData {
  items: NodeData[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

/** 列出节点查询参数 */
export interface ListNodesParams {
  /** 集群ID */
  cluster_id?: number
  /** 节点状态，不传则列出全部 */
  status?: NodeStatus
  /** 页码，从1开始 */
  page?: number
  /** 每页条数，最大100 */
  page_size?: number
}

/** 列出节点（GET /api/v1/admin/nodes）；meta 可选，静默轮询传 { silentError: true } 抑制统一错误提示 */
export function listNodes(
  params: ListNodesParams,
  signal?: AbortSignal,
  meta?: HttpRequestMeta,
): Promise<NodePageData> {
  return request({ url: '/admin/nodes', method: 'GET', params, signal, meta })
}

/** 获取节点详情（GET /api/v1/admin/nodes/{node_id}） */
export function getNode(nodeId: string): Promise<NodeData> {
  return request({ url: `/admin/nodes/${nodeId}`, method: 'GET' })
}

/** 更新节点请求体：均为可选，不传则不修改（硬件信息由节点上报，不可手改） */
export interface UpdateNodePayload {
  /** 节点备注 */
  remark?: string
  /** 节点状态调度 */
  status?: NodeStatus
}

/** 更新节点（POST /api/v1/admin/nodes/{node_id}/update） */
export function updateNode(nodeId: string, data: UpdateNodePayload): Promise<NodeData> {
  return request({ url: `/admin/nodes/${nodeId}/update`, method: 'POST', data })
}

/**
 * 注销回执（对应后端 NodeDeleteData）。成功返回即删除完成；
 * force 强制移除时 notified/cleaned 恒为 false（节点已离线，既不发自清理通知也等不到确认）。
 */
export interface NodeDeleteData {
  /** 节点ID */
  id: string
  /** 删除是否生效（成功返回即已完成） */
  deleted: boolean
  /** 是否向在线节点下发过自清理通知（强制移除恒为 false） */
  notified: boolean
  /** 节点是否确认自清理（离线节点/超时为 false） */
  cleaned: boolean
  /** 本次连带删除的部署ID；普通注销恒为空数组 */
  deleted_deployments: string[]
}

/** 注销节点参数：缺省即原来的注销行为 */
export interface DeleteNodeOptions {
  /** 强制移除：跳过「存在未停止的推理实例」守卫，级联删除该节点上的部署；仅离线节点生效 */
  force?: boolean
}

/**
 * 注销节点（POST /api/v1/admin/nodes/{node_id}/delete?force=）。
 * 普通注销：在线节点先收 $node_decommission 自清理再删，节点带未停止实例时后端 409。
 * force=true：给机器报废/长期失联的节点用，不看实例状态直接删，并级联删除该节点上的部署；
 * 节点仍在线时后端 409（活着就该走普通注销，让节点清掉自己的容器与数据卷）。
 */
export function deleteNode(nodeId: string, options?: DeleteNodeOptions): Promise<NodeDeleteData> {
  return request({
    url: `/admin/nodes/${nodeId}/delete`,
    method: 'POST',
    // force 只在强制移除时带上：不带该参数与旧行为完全一致
    params: options?.force ? { force: true } : undefined,
  })
}

/** 节点升级下发结果（POST /api/v1/admin/nodes/{node_id}/upgrade） */
export interface NodeUpgradeItem {
  /** 节点ID */
  id: string
  /** 升级指令是否已下发（true 仅代表送达，不代表升级完成） */
  dispatched: boolean
  /** 未下发原因（中文可直接展示，如「节点不在线」；下发成功时为 null） */
  reason: string | null
}

/**
 * 下发节点升级（POST /api/v1/admin/nodes/{node_id}/upgrade，无 body）。
 * 返回只代表指令送达：完成信号 = 列表 version_status 翻 ok（镜像预载 <30s，需拉取则分钟级），
 * 节点 status 全程 online 无「升级中」态；节点侧失败（拉取失败/回滚/存量节点不支持）只在审计页。
 */
export function upgradeNode(nodeId: string): Promise<NodeUpgradeItem> {
  return request({ url: `/admin/nodes/${nodeId}/upgrade`, method: 'POST' })
}

/** 签发节点引导令牌请求体（POST /api/v1/admin/nodes/bootstrap-token）；传参原则：未修改的字段不传，空值传 null 不传空串 */
export interface BootstrapTokenPayload {
  /** 目标集群 ID（必填，须存在且 active，否则 404） */
  cluster_id: number
  /** 令牌有效期（秒），后端 clamp 到 [60, 86400]，缺省 28800（8h） */
  ttl?: number
  /** 节点互联 IP（IPv4/IPv6；其他节点访问本机的地址，bridge 网络必填，host 网络留空自动探测） */
  node_ip?: string
  /** 节点容器名（缺省 llmpod-node，同机多节点时才需要改） */
  container_name?: string
  /** 节点数据卷：docker 具名卷或宿主绝对路径（节点身份持久化于此，重装复用；缺省 llmpod-node-data） */
  data_volume?: string
  /** 宿主侧模型目录（大盘挂在别处时才需要填；容器内路径恒为 Manager 配置，留空与其对齐） */
  model_host_dir?: string
  /** 额外挂载列表，每条 src:dst[:mode]，最多 16 条不可重复（典型：Ascend NPU 驱动目录） */
  extra_mounts?: string[]
  /** 节点镜像 ref（私有 registry 时填全；缺省 llmpod:latest） */
  image?: string
}

/** 签发结果（对应后端 BootstrapTokenData） */
export interface BootstrapTokenData {
  /** 回显目标集群 ID */
  cluster_id: number | null
  /** 短期 HMAC 引导令牌（已拼进 command，一般无需单独展示） */
  token: string | null
  /** 令牌过期时间（ISO8601 UTC，展示需转本地时区） */
  expires_at: string | null
  /** 后端拼好的完整可粘贴 curl 命令（目标机器 shell 直接执行，前端只展示/复制，不自行拼接） */
  command: string | null
}

/**
 * 签发节点引导令牌（POST /api/v1/admin/nodes/bootstrap-token）。
 * 返回的 command 引导脚本可重复执行（幂等：同名容器先删再起，node_id 存数据卷身份不变）；
 * 令牌有效期内可重复使用（可引导多台机器）。
 */
export function createBootstrapToken(data: BootstrapTokenPayload): Promise<BootstrapTokenData> {
  return request({ url: '/admin/nodes/bootstrap-token', method: 'POST', data })
}

/** 生成节点注册演示命令(仅 quick-setup 向导 mock 模式(VITE_USE_SETUP_MOCK=true,无后端)兜底;真实命令一律走 bootstrap-token 接口由后端拼接) */
export function buildRegisterCommand(pool: string): string {
  return [
    `curl -fsSL 'https://api.ai-platform.com/agent/install.sh' | sudo bash -s -- \\`,
    `  --server=api.ai-platform.com:8443 \\`,
    `  --pool=${pool} \\`,
    `  --token=$(uuidgen)`,
  ].join('\n')
}
