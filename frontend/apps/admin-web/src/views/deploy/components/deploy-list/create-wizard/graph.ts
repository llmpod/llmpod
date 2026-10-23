import type { Edge, Node } from '@vue-flow/core'
import type { ClusterTopology } from '@/api/topology'

/** 选择图节点类型:集群 → 节点 → GPU 三列 */
export type SelectGraphNodeType = 'cluster' | 'node' | 'gpu'

/** 列 x 坐标(flow 坐标系) */
const COLUMN_X = { cluster: 0, node: 260, gpu: 520 }
/** 行距需大于最高卡片(GPU 卡含显存/利用率/评估/已选角标,约 17rem),否则相邻卡边框重叠 */
const ROW_GAP = 180

/** 列内纵向均布并整体垂直居中(与 node-pool/topology graph.ts 同算法) */
function columnY(index: number, total: number): number {
  return (index - (total - 1) / 2) * ROW_GAP
}

export interface BuiltSelectGraph {
  nodes: Node[]
  edges: Edge[]
}

/** 静态结构线(颜色由步骤组件的 .edge-static 样式控制) */
function edge(source: string, target: string): Edge {
  return { id: `e-${source}-${target}`, source, target, class: 'edge-static' }
}

/**
 * 由单集群拓扑组装选择图(纯函数)。
 * 选中/禁用态不在此注入,由卡片组件从向导 store 实时读,避免选择变化重建整张图。
 * GPU 图节点按 uuid 寻址,与部署 gpu_uuids 对应。
 */
export function buildSelectGraph(data: ClusterTopology): BuiltSelectGraph {
  const flowNodes: Node[] = []
  const flowEdges: Edge[] = []
  const clusterId = `cluster-${data.cluster.id}`

  flowNodes.push({
    id: clusterId,
    type: 'cluster',
    position: { x: COLUMN_X.cluster, y: 0 },
    // 框选只针对 GPU 卡,集群/节点卡不参与 Vue Flow 选区
    selectable: false,
    data: {
      record: { ...data.cluster, nodeCount: data.nodes.length, gpuTotal: data.gpus.length },
    },
  })

  data.nodes.forEach((nodeItem, index) => {
    const id = `node-${nodeItem.id}`
    flowNodes.push({
      id,
      type: 'node',
      position: { x: COLUMN_X.node, y: columnY(index, data.nodes.length) },
      selectable: false,
      data: { record: nodeItem },
    })
    flowEdges.push(edge(clusterId, id))
  })

  data.gpus.forEach((gpu, index) => {
    const id = `gpu-${gpu.uuid}`
    flowNodes.push({
      id,
      type: 'gpu',
      position: { x: COLUMN_X.gpu, y: columnY(index, data.gpus.length) },
      data: { record: gpu },
    })
    flowEdges.push(edge(`node-${gpu.node_id}`, id))
  })

  return { nodes: flowNodes, edges: flowEdges }
}
