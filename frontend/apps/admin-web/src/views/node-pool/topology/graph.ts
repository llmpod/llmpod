import type { Edge, Node } from '@vue-flow/core'
import type {
  AggregateNodeData,
  ClusterGraphData,
  ClusterNodeData,
  DeployNodeData,
  EngineNodeData,
  FlowDeploy,
  FlowGpu,
  FlowModel,
  GpuNodeData,
  NodeNodeData,
} from './useStore'

/** 视图模式:compact=聚合主干;detail=实体铺开 */
export type ViewMode = 'compact' | 'detail'

/** 画布节点类型:6 类实体 + 聚合块 */
export type GraphNodeType = 'cluster' | 'node' | 'gpu' | 'deploy' | 'engine' | 'model' | 'aggregate'

/** 详情面板选中态:节点类型 + 原始记录 */
export interface PanelSelection {
  type: GraphNodeType
  record: unknown
}

/** 列 x 坐标(flow 坐标系):详细模式 6 列;简洁模式 node/gpu 折叠为 aggregate */
const COLUMN_X = { cluster: 0, node: 260, gpu: 520, deploy: 780, engine: 1040, model: 1300 }
const COMPACT_X = { cluster: 0, aggregate: 260, deploy: 520, engine: 780, model: 1040 }
const ROW_GAP = 140
/** 节点卡含 CPU/内存占用条(约 18rem 高),行距需大于卡高,否则相邻卡边框重叠 */
const NODE_ROW_GAP = 200

/** 列内纵向均布并整体垂直居中 */
function columnY(index: number, total: number, gap = ROW_GAP): number {
  return (index - (total - 1) / 2) * gap
}

/** 文本稳定序(UTF-16 码元比较,与 locale 无关,跨轮询确定性) */
function byText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

function entityNode(
  id: string,
  type: GraphNodeType,
  x: number,
  y: number,
  record: unknown,
  detail: boolean,
): Node {
  return { id, type, position: { x, y }, data: { record, detail } }
}

/** 静态结构线 / 推理链路线(颜色由 index.vue 的 .edge-static/.edge-flow 样式控制) */
function edge(source: string, target: string, animated: boolean): Edge {
  return {
    id: `e-${source}-${target}`,
    source,
    target,
    animated,
    class: animated ? 'edge-flow' : 'edge-static',
  }
}

export interface BuiltGraph {
  nodes: Node[]
  edges: Edge[]
}

/** 由集群数据 + 视图模式组装 Vue Flow 节点与连线(纯函数) */
export function buildGraph(data: ClusterGraphData, mode: ViewMode): BuiltGraph {
  const flowNodes: Node[] = []
  const flowEdges: Edge[] = []
  const clusterId = `cluster-${data.cluster.id}`
  const detail = mode === 'detail'

  // 各列按自然键稳定排序:y 落位依赖数组下标,接口返回顺序逐轮抖动会让相同 id 跳行,
  // 轮询刷新时打断浏览;排序后同集合同落位,只有实体增减才移动
  const nodeList = [...data.nodes].sort((a, b) => byText(a.hostname, b.hostname) || byText(a.id, b.id))
  const gpuList = [...data.gpus].sort((a, b) => byText(a.nodeId, b.nodeId) || a.id - b.id)
  const deployList = [...data.deploys].sort((a, b) => byText(a.name, b.name) || byText(a.id, b.id))
  const engineList = [...data.engines].sort((a, b) => byText(a.key, b.key))
  const modelList = [...data.models].sort((a, b) => byText(a.name, b.name) || a.id - b.id)

  const engineOf = (deploy: FlowDeploy) => engineList.find((item) => item.key === deploy.engineKey)
  const modelOf = (deploy: FlowDeploy) => modelList.find((item) => item.id === deploy.modelId)
  const gpusOfNode = (nodeId: string) => gpuList.filter((item) => item.nodeId === nodeId)
  const hostOfGpu = (gpu: FlowGpu) =>
    nodeList.find((item) => item.id === gpu.nodeId)?.hostname ?? `#${gpu.nodeId}`

  // 集群列(始终展示聚合统计)
  const clusterRecord: ClusterNodeData = {
    ...data.cluster,
    nodeCount: nodeList.length,
    gpuTotal: gpuList.length,
  }
  flowNodes.push(entityNode(clusterId, 'cluster', detail ? COLUMN_X.cluster : COMPACT_X.cluster, 0, clusterRecord, detail))

  if (!detail) {
    // 简洁模式:节点/GPU 折叠为聚合块
    const aggregateRecord: AggregateNodeData = {
      nodeCount: nodeList.length,
      gpuTotal: gpuList.length,
      onlineNodes: nodeList.filter((item) => item.status === 'online').length,
    }
    const aggregateId = `aggregate-${data.cluster.id}`
    flowNodes.push(entityNode(aggregateId, 'aggregate', COMPACT_X.aggregate, 0, aggregateRecord, false))
    flowEdges.push(edge(clusterId, aggregateId, false))
    for (const deploy of deployList) {
      flowEdges.push(edge(aggregateId, `deploy-${deploy.id}`, deploy.status === 'running'))
    }
  } else {
    // 详细模式:节点、GPU 全部铺开(GPU 图节点按 uuid 寻址,与部署 gpuUuids 对应)
    const allGpuUuids = new Set(gpuList.map((item) => item.uuid))
    nodeList.forEach((nodeItem, index) => {
      const nodeGpus = gpusOfNode(nodeItem.id)
      const gpuUuidSet = new Set(nodeGpus.map((item) => item.uuid))
      const nodeDeploys = deployList.filter((deploy) => deploy.gpuUuids.some((uuid) => gpuUuidSet.has(uuid)))
      const nodeRecord: NodeNodeData = {
        ...nodeItem,
        gpus: nodeGpus,
        deployCount: nodeDeploys.length,
        deployNames: nodeDeploys.map((deploy) => deploy.name),
        // 承载副本:各部署副本摘要中落点含本节点的份(副本N · 部署名);后端 seq 为 0 基,展示 +1
        replicaRefs: nodeDeploys.flatMap((deploy) =>
          deploy.replicaSummaries
            .filter((summary) => summary.nodeIds.includes(nodeItem.id))
            .map((summary) => `副本${summary.seq + 1} · ${deploy.name}`),
        ),
      }
      const id = `node-${nodeItem.id}`
      flowNodes.push(entityNode(id, 'node', COLUMN_X.node, columnY(index, nodeList.length, NODE_ROW_GAP), nodeRecord, true))
      flowEdges.push(edge(clusterId, id, false))
    })
    gpuList.forEach((gpu, index) => {
      const gpuRecord: GpuNodeData = { ...gpu, nodeHost: hostOfGpu(gpu) }
      const id = `gpu-${gpu.uuid}`
      flowNodes.push(entityNode(id, 'gpu', COLUMN_X.gpu, columnY(index, gpuList.length), gpuRecord, true))
      flowEdges.push(edge(`node-${gpu.nodeId}`, id, false))
    })
    for (const deploy of deployList) {
      for (const gpuUuid of deploy.gpuUuids) {
        if (!allGpuUuids.has(gpuUuid)) continue
        flowEdges.push(edge(`gpu-${gpuUuid}`, `deploy-${deploy.id}`, deploy.status === 'running'))
      }
    }
  }

  // 部署/引擎/模型列(两种模式一致,running 部署链路流动画)
  const deployX = detail ? COLUMN_X.deploy : COMPACT_X.deploy
  const engineX = detail ? COLUMN_X.engine : COMPACT_X.engine
  const modelX = detail ? COLUMN_X.model : COMPACT_X.model

  deployList.forEach((deploy, index) => {
    const engine = engineOf(deploy)
    const model = modelOf(deploy)
    const deployRecord: DeployNodeData = {
      ...deploy,
      engineName: engine ? `${engine.name} ${engine.version}`.replace(' --', '') : '--',
      modelName: model?.name ?? '--',
    }
    const id = `deploy-${deploy.id}`
    flowNodes.push(entityNode(id, 'deploy', deployX, columnY(index, deployList.length), deployRecord, detail))
    if (engine) flowEdges.push(edge(id, `engine-${engine.key}`, deploy.status === 'running'))
    if (model) flowEdges.push(edge(id, `model-${deploy.modelId}`, deploy.status === 'running'))
  })
  engineList.forEach((engine, index) => {
    const engineRecord: EngineNodeData = {
      ...engine,
      deployCount: deployList.filter((deploy) => deploy.engineKey === engine.key).length,
    }
    flowNodes.push(entityNode(`engine-${engine.key}`, 'engine', engineX, columnY(index, engineList.length), engineRecord, detail))
  })
  modelList.forEach((model: FlowModel, index) => {
    flowNodes.push(entityNode(`model-${model.id}`, 'model', modelX, columnY(index, modelList.length), { ...model }, detail))
  })

  return { nodes: flowNodes, edges: flowEdges }
}
