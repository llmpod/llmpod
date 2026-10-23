import { createCluster, listClusters, type ClusterData, type ClusterType } from '@/api/cluster'
import {
  getDeployment,
  type DeploymentStatus,
  type ModelDeploymentOut,
} from '@/api/deployment'
import { listNodes, type NodeData, type NodeStatus } from '@/api/node'
import { listModels, type ModelData } from '@/api/model'
import {
  isSetupMock,
  markClusterCreated,
  QUICK_SETUP_DONE_KEY,
  uploadModel as apiUploadModel,
  type DeployStatus,
} from '@/api/quick-setup'

export type { ClusterType, DeployStatus }

/** 步骤1 表单值 */
export interface ClusterFormValues {
  name: string
  type: ClusterType
  description: string
}

/** 向导节点:集群内已注册节点(来源 listNodes 轮询) */
export interface WizardNode {
  id: string
  hostname: string
  ip: string
  status: NodeStatus
  /** 硬件概要,如 "8C / 32G";未知为 '' */
  hardware: string
}

/** 已上传模型 */
export interface WizardModel {
  id: string
  name: string
  sizeText: string
}

/** 集群类型选项 */
export const CLUSTER_TYPE_OPTIONS: { label: string; value: ClusterType }[] = [
  { label: 'Docker', value: 'docker' },
  { label: 'Kubernetes', value: 'k8s' },
]

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 文件大小格式化(B/KB/MB/GB) */
function formatSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

/** 由节点上报信息拼硬件概要 */
function formatHardware(node: NodeData): string {
  const parts: string[] = []
  if (node.cpu_cores != null) parts.push(`${node.cpu_cores}C`)
  if (node.memory_gb != null) parts.push(`${node.memory_gb}G`)
  return parts.join(' / ')
}

/* ---------------- 模块级单例状态(向导页与步骤组件共享) ---------------- */

const currentStep = ref(1)
const cluster = ref<ClusterData | null>(null)
/** 全部集群(init 拉全量;多集群时步骤1 提供下拉切换,单集群时仅展示名称) */
const clusters = ref<ClusterData[]>([])
const nodes = ref<WizardNode[]>([])
const model = ref<WizardModel | null>(null)
const modelSkipped = ref(false)
const uploading = ref(false)
const uploadProgress = ref(0)
const deploy = ref<{ id: string; name: string; status: DeployStatus } | null>(null)

/** 在线节点(步骤1 门控/完成页统计) */
const onlineNodes = computed(() => nodes.value.filter((node) => node.status === 'online'))

/** mock 注册 tick 计数(集群就绪 2 个 tick 后模拟一台节点注册上线) */
let mockRegisterTicks = 0

/** tick 重入保护:慢响应时跳过重叠 tick */
let tickRunning = false

/* ---------------- 轮询 ---------------- */

let pollTimer: number | null = null

/** NodeData → 向导节点 */
function toWizardNode(node: NodeData): WizardNode {
  return {
    id: node.id,
    hostname: node.hostname,
    ip: node.ip,
    status: node.status,
    hardware: formatHardware(node),
  }
}

/** 单个 tick:步骤1(添加节点)同步集群已注册节点(真实走 listNodes,mock 模拟一台上线);部署中任务状态全步骤轮询 */
async function tick(): Promise<void> {
  // 节点列表只在步骤1 需要(门控/列表展示),进入后续步骤后停止,避免无效轮询
  if (currentStep.value === 1) {
    if (isSetupMock()) {
      if (cluster.value && nodes.value.length === 0) {
        mockRegisterTicks += 1
        if (mockRegisterTicks >= 2) {
          nodes.value = [
            { id: 'node-mock', hostname: 'demo-node', ip: '192.168.1.10', status: 'online', hardware: '8C / 32G' },
          ]
        }
      }
    }
    else if (cluster.value) {
      try {
        const nodePage = await listNodes({ cluster_id: cluster.value.id, page: 1, page_size: 100 })
        nodes.value = nodePage.items.map(toWizardNode)
      }
      catch {
        // 单次轮询失败不改状态,下个 tick 重试
      }
    }
  }
  // 部署状态轮询:走真实部署详情接口(步骤3 已切换为新建部署向导流程,mock 不再模拟部署)
  if (deploy.value?.status === 'deploying') {
    try {
      const detail = await getDeployment(deploy.value.id)
      deploy.value.status = toWizardDeployStatus(detail.deployment.status)
    }
    catch {
      // 同上,下个 tick 重试
    }
  }
}

function startPolling(): void {
  if (pollTimer !== null) return
  pollTimer = window.setInterval(() => {
    if (tickRunning) return
    tickRunning = true
    void tick().finally(() => {
      tickRunning = false
    })
  }, 2000)
}

function stopPolling(): void {
  if (pollTimer === null) return
  clearInterval(pollTimer)
  pollTimer = null
}

/* ---------------- 动作 ---------------- */

/** 默认集群名称(无集群进入向导时自动创建) */
const DEFAULT_CLUSTER_NAME = '默认集群'

/** mock 集群 id 自增(同一会话内可多次新建) */
let mockClusterSeq = 0

/** 构造 mock 集群 */
function buildMockCluster(values: ClusterFormValues): ClusterData {
  mockClusterSeq += 1
  return {
    id: mockClusterSeq,
    name: values.name,
    type: values.type,
    description: values.description || null,
    status: 'active',
    spec: {},
    created_at: '',
    updated_at: '',
    node_count: 0,
    online_node_count: 0,
    gpu_count: 0,
  }
}

/**
 * 初始化:确保存在一个集群(没有则自动创建默认集群),向导第一步直接进入注册节点。
 * 拉取全量集群:仅一个时直接选中(开箱即用),多个时步骤1 提供下拉切换,默认选中第一个。
 * 真实模式下回填选中集群已注册节点与最新模型(此前已添加过则直接进入后续步骤);
 * mock 模式集群在内存中补齐即可。
 */
async function init(): Promise<void> {
  if (isSetupMock()) {
    if (!cluster.value) {
      cluster.value = buildMockCluster({ name: DEFAULT_CLUSTER_NAME, type: 'docker', description: '' })
      clusters.value = [cluster.value]
      markClusterCreated()
    }
    return
  }
  const clusterPage = await listClusters({ page: 1, page_size: 100 })
  clusters.value = clusterPage.items
  const existing = clusterPage.items[0]
  if (!existing) {
    cluster.value = await createCluster({ name: DEFAULT_CLUSTER_NAME, type: 'docker' })
    clusters.value = [cluster.value]
    markClusterCreated()
    return
  }
  cluster.value = existing
  const nodePage = await listNodes({ cluster_id: existing.id, page: 1, page_size: 100 })
  nodes.value = nodePage.items.map(toWizardNode)
  // 已添加过模型时回填最新一条,步骤2 直接展示可跳过
  if (!model.value) {
    const modelPage = await listModels({ page: 1, page_size: 1 })
    const latest = modelPage.items[0]
    if (latest) applyModel(latest)
  }
}

/** 复位向导全部状态(模块级单例二次进入前调用,避免残留状态导致步骤错乱) */
function reset(): void {
  stopPolling()
  currentStep.value = 1
  cluster.value = null
  clusters.value = []
  nodes.value = []
  model.value = null
  modelSkipped.value = false
  uploading.value = false
  uploadProgress.value = 0
  deploy.value = null
  mockRegisterTicks = 0
}

/** 步骤切换(1-4 区间收敛;到达完成页写入「向导已完成」标记,守卫据此防回退) */
function goToStep(step: number): void {
  const target = Math.min(4, Math.max(1, step))
  currentStep.value = target
  if (target >= 4) sessionStorage.setItem(QUICK_SETUP_DONE_KEY, '1')
}

/** 「新建集群」提交:总是创建新集群并切换上下文,原集群的节点/部署状态随之清空 */
async function submitCluster(values: ClusterFormValues): Promise<void> {
  if (isSetupMock()) {
    cluster.value = buildMockCluster(values)
  }
  else {
    cluster.value = await createCluster({
      name: values.name,
      type: values.type,
      description: values.description || undefined,
    })
  }
  // 新集群入列并保持选中(多集群下拉随之出现新选项)
  clusters.value = [...clusters.value, cluster.value]
  nodes.value = []
  mockRegisterTicks = 0
  deploy.value = null
  markClusterCreated()
}

/** 切换当前集群(步骤1 多集群下拉):上下文随切清空节点/部署状态,并按新集群重新拉取节点 */
async function switchCluster(clusterId: number): Promise<void> {
  const target = clusters.value.find((item) => item.id === clusterId)
  if (!target || target.id === cluster.value?.id) return
  cluster.value = target
  nodes.value = []
  mockRegisterTicks = 0
  deploy.value = null
  if (isSetupMock()) return
  try {
    const nodePage = await listNodes({ cluster_id: target.id, page: 1, page_size: 100 })
    // 慢响应回来前又切走则丢弃,避免旧集群节点覆盖当前列表
    if (cluster.value?.id === target.id) nodes.value = nodePage.items.map(toWizardNode)
  }
  catch {
    // 单次拉取失败保持空列表,由轮询 tick 重试
  }
}

/** 步骤2:添加模型向导创建成功后回写(模型记录即部署用的 model_id 来源) */
function applyModel(record: ModelData): void {
  model.value = {
    id: String(record.id),
    name: record.name,
    sizeText: record.size_gb != null ? `${record.size_gb} GB` : '',
  }
  // 补传成功后复位「稍后上传」标记,步骤4 方可提交部署
  modelSkipped.value = false
}

/** 步骤2:上传模型(仅 mock 演示用;真实模式走添加模型向导,见 applyModel) */
async function uploadModelFile(file: File): Promise<void> {
  if (uploading.value) return
  uploading.value = true
  uploadProgress.value = 0
  try {
    if (isSetupMock()) {
      while (uploadProgress.value < 100) {
        await delay(200)
        uploadProgress.value = Math.min(100, uploadProgress.value + 8)
      }
      model.value = { id: `model-${Date.now()}`, name: file.name, sizeText: formatSize(file.size) }
    }
    else {
      const result = await apiUploadModel(file)
      model.value = { id: result.id, name: result.name, sizeText: result.size_text }
    }
    // 补传成功后复位「稍后上传」标记,步骤4 方可提交部署
    modelSkipped.value = false
  }
  finally {
    uploading.value = false
  }
}

/** 步骤3:稍后在模型管理上传(步骤4 将不可提交部署) */
function skipModelUpload(): void {
  modelSkipped.value = true
}

/** 控制平面部署状态 → 向导展示态(creating 归并为部署中;stopped/deleting 归并为失败) */
function toWizardDeployStatus(status: DeploymentStatus): DeployStatus {
  if (status === 'running') return 'running'
  if (status === 'creating') return 'deploying'
  return 'failed'
}

/** 步骤3 部署创建成功后回写(完成页展示;轮询 tick 负责推进状态) */
function applyDeploy(record: ModelDeploymentOut): void {
  deploy.value = {
    id: record.deployment_id,
    name: record.name,
    status: toWizardDeployStatus(record.status),
  }
}

/**
 * 向导数据 store:模块级单例,向导页与各步骤组件共享同一份状态。
 * 返回的是装 ref 的普通对象——组件里解构使用,script 中访问需 .value。
 * mock 开关见 api/quick-setup.ts 的 isSetupMock()。
 */
export function useStore() {
  return {
    currentStep,
    cluster,
    clusters,
    nodes,
    model,
    modelSkipped,
    uploading,
    uploadProgress,
    deploy,
    onlineNodes,
    init,
    reset,
    goToStep,
    submitCluster,
    switchCluster,
    applyModel,
    uploadModelFile,
    skipModelUpload,
    applyDeploy,
    startPolling,
    stopPolling,
  }
}
