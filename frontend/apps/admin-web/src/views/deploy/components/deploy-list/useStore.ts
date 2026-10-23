import { shallowRef } from 'vue'
import type { TableRequestContext } from '@repo/hooks'
import {
  createDeployment,
  deleteDeployment,
  getDeployment,
  listDeployments,
  normalizedTopology,
  startDeployment,
  stopDeployment,
  type CreateDeploymentPayload,
  type DeploymentDetailOut,
  type DeploymentStatus,
  type ModelDeploymentOut,
} from '@/api/deployment'
import { listModels } from '@/api/model'

/** 部署列表行记录(即后端 ModelDeploymentSchema) */
export type DeployRecord = ModelDeploymentOut

/** 状态 → 标签配色(对应后端 status 状态机:creating/running/failed/stopped/deleting) */
export const deployStatusMap: Record<
  DeploymentStatus,
  { label: string; type: 'success' | 'warning' | 'error' | 'default' }
> = {
  creating: { label: '部署中', type: 'warning' },
  running: { label: '运行中', type: 'success' },
  failed: { label: '失败', type: 'error' },
  stopped: { label: '已停止', type: 'default' },
  deleting: { label: '删除中', type: 'warning' },
}

/** 状态筛选选项(空值为全部) */
export const DEPLOY_STATUS_OPTIONS = [
  { label: '全部状态', value: '' },
  { label: '部署中', value: 'creating' },
  { label: '运行中', value: 'running' },
  { label: '失败', value: 'failed' },
  { label: '已停止', value: 'stopped' },
]

/** 编排子态 → 进度文案(creating 期推进;STOPPING 停止时插入;SCALING 在线扩缩容时插入) */
export const DEPLOY_PHASE_LABEL: Record<string, string> = {
  PENDING: '排队中',
  STAGING_MODEL: '下载模型',
  PULL_IMAGE: '拉取镜像',
  START_HEAD: '启动主节点',
  WAIT_HEAD_READY: '主节点健康检查',
  START_WORKERS: '启动工作节点',
  WAIT_WORKERS_READY: '工作节点健康检查',
  FINALIZING: '生成端点',
  RUNNING: '就绪',
  STOPPING: '停止中',
  SCALING: '扩缩容中',
}

/**
 * 创建流水线有序子态(用于步骤进度「n/总数」与悬浮步骤条);
 * STOPPING 属拆除流程,不计入创建进度。
 */
export const DEPLOY_CREATE_PHASES: string[] = [
  'PENDING',
  'STAGING_MODEL',
  'PULL_IMAGE',
  'START_HEAD',
  'WAIT_HEAD_READY',
  'START_WORKERS',
  'WAIT_WORKERS_READY',
  'FINALIZING',
  'RUNNING',
]

/** 创建步骤条选项(label 对齐 DEPLOY_PHASE_LABEL 文案) */
export const DEPLOY_PHASE_STEPS = DEPLOY_CREATE_PHASES.map((key) => ({
  key,
  label: DEPLOY_PHASE_LABEL[key],
}))

/**
 * 算力概要(单副本规格):「N 节点 · M 卡」,总卡数 = M × 副本数(副本数单列展示)。
 * 多副本可共用节点,总节点数无法从 spec 推出,只给单副本口径;无拓扑信息时显示 --。
 * 纯 CPU 部署(0 卡)按 CPU 档口径展示,不虚报 GPU 卡数。
 */
export function gpuSummary(row: DeployRecord): string {
  const spec = row.spec
  const topo = spec?.topology
  // 纯 CPU 部署:offload_preference=cpu_only(或 allow_cpu 且未手选任何卡)→ 0 GPU,按 CPU 口径
  if (
    spec?.offload_preference === 'cpu_only' ||
    (spec?.allow_cpu && !spec?.gpu_selector?.gpu_uuids?.length)
  ) {
    return '纯 CPU · 1 节点'
  }
  if (!topo || (!topo.tp && !topo.units?.length)) return '--'
  const { gpusPerEngine, units } = normalizedTopology(topo)
  const nnodes = units[0]?.nnodes ?? 1
  // NaN 兜底(后端 units 缺 tp/pp/ep 字段时):不展示「NaN 卡」
  if (!Number.isFinite(gpusPerEngine)) return '--'
  return `${nnodes} 节点 · ${gpusPerEngine} 卡`
}

/** ISO8601 时间格式化为 YYYY-MM-DD HH:mm;空值显示 —(同 model-management 口径) */
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

/** 模型 id → 名称映射(列表/详情回显模型名);懒加载一次,失败回退 #id 展示 */
const modelNameMap = shallowRef<Map<number, string>>(new Map())
let modelNameLoaded = false

/**
 * 部署数据 store:表格请求与写操作均走此单例,
 * 数据来源于 /api/v1/admin/model-deployments 接口;
 * 写操作内部 notify → 列表自动刷新,无需父级回调。
 */
export function useStore() {
  /** 分页请求(供 useTable.request),支持状态筛选 */
  const request = async (
    params: { page: number; pageSize: number; status?: string },
    ctx?: TableRequestContext,
  ) => {
    // useTable 按 config.request.pageSizeField 透传,运行时为 page_size
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    const pageData = await listDeployments(
      { page: params.page, page_size: pageSize, status: params.status || undefined },
      ctx?.signal,
    )
    return { data: pageData.items, total: pageData.total }
  }

  /** 加载模型名映射(幂等);失败静默,HTTP 层已统一提示,展示回退 #id */
  const loadModelNames = async (force = false): Promise<void> => {
    if (modelNameLoaded && !force) return
    try {
      const res = await listModels({ page: 1, page_size: 100 })
      modelNameMap.value = new Map(res.items.map((item) => [item.id, item.name]))
      modelNameLoaded = true
    } catch {
      // 名称映射缺失不阻塞列表,模型列回退显示 #id
    }
  }

  /** 模型名回显:未加载/未命中时回退 #id */
  const modelName = (modelId: number): string => modelNameMap.value.get(modelId) ?? `#${modelId}`

  /** 部署详情(deployment + instances + endpoint) */
  const fetchDetail = (deploymentId: string): Promise<DeploymentDetailOut> =>
    getDeployment(deploymentId)

  /** 新建部署(POST /admin/model-deployments):落库 status=creating 立即返回,进度靠列表轮询 */
  const createDeploy = async (input: CreateDeploymentPayload) => {
    const result = await createDeployment(input)
    notify()
    return result
  }

  /** 停止部署:前端仅对 running/creating 提供入口(failed 已是停止终态),成功后 notify 联动刷新 */
  const stop = async (deploymentId: string): Promise<void> => {
    await stopDeployment(deploymentId)
    notify()
  }

  /** 启动(重启)部署:仅 stopped/failed;原卡被占时接口 409,由调用方确认后 force 重试;成功后 notify 联动刷新 */
  const start = async (deploymentId: string, force = false): Promise<void> => {
    await startDeployment(deploymentId, force)
    notify()
  }

  /** 删除部署:标 deleting 后级联清理,成功后 notify 联动刷新 */
  const remove = async (deploymentId: string): Promise<void> => {
    await deleteDeployment(deploymentId)
    notify()
  }

  /** 批量删除:逐个标 deleting 级联清理,全部完成后统一 notify 联动刷新 */
  const removeMany = async (deploymentIds: string[]): Promise<void> => {
    await Promise.all(deploymentIds.map((id) => deleteDeployment(id)))
    notify()
  }

  /** 订阅列表变更;返回取消函数,调用方在 onBeforeUnmount 取消 */
  const subscribe = (fn: () => void) => {
    listeners.add(fn)
    return () => listeners.delete(fn)
  }

  return {
    request,
    loadModelNames,
    modelName,
    fetchDetail,
    createDeploy,
    stop,
    start,
    remove,
    removeMany,
    subscribe,
  }
}
