import type { Edge, Node } from '@vue-flow/core'
import { useNodesInitialized, useVueFlow } from '@vue-flow/core'
import { buildGraph, type GraphNodeType, type PanelSelection, type ViewMode } from './graph'
import { POLL_INTERVAL_MS, useStore, type ClusterGraphData } from './useStore'

export interface TopologyControllerOptions {
  /** 入口集群 id(node-pool 行记录),仅 active 集群有拓扑数据,故再按名称兜底 */
  clusterId?: number
  clusterName?: string
  /** 退出拓扑视图(返回表格) */
  onExit: () => void
}

export function useController(options: TopologyControllerOptions) {
  const { fetchTopologies } = useStore()
  const router = useRouter()
  const { onExit } = options

  /** 拓扑数据缓存:clusterId → 图数据(一次拉取全部 active 集群,切换零请求;轮询整体替换) */
  const graphMap = shallowRef<Map<number, ClusterGraphData>>(new Map())
  const loading = ref(true)

  /** 工具栏状态:当前集群 + 视图模式 */
  const currentClusterId = ref(options.clusterId ?? 0)
  /** 默认进入详细视图(简洁模式仍可手动切换) */
  const mode = ref<ViewMode>('detail')
  /** 详情面板选中节点 id:record 经 nodes 实时派生(见下方 selection computed) */
  const selectedNodeId = ref('')
  /** 悬停节点 id:驱动连线高亮/淡出 */
  const hoverNodeId = ref('')

  const nodes = shallowRef<Node[]>([])
  const edges = shallowRef<Edge[]>([])

  const { onNodeClick, onPaneClick, onNodeMouseEnter, onNodeMouseLeave, fitView } = useVueFlow()
  /** 全部节点完成尺寸测量后为 true;新节点先渲染、再由 ResizeObserver 异步回填尺寸 */
  const nodesInitialized = useNodesInitialized()

  /** 待处理的视图适配请求计数:节点尺寸就绪后由下方 watch 消费,合并多次切换为一次适配 */
  let pendingFit = 0

  /**
   * 节点尺寸就绪后消费待处理的适配请求。
   * 此 watch 在 setup 同步段注册,随组件作用域自动 stop;替代旧实现里跨 await 注册、脱离作用域的游离 watcher(卸载泄漏,F-003)。
   */
  watch(nodesInitialized, (ready) => {
    if (ready && pendingFit > 0) {
      pendingFit = 0
      fitView({ padding: 0.2, duration: 300 })
    }
  })

  /** 集群/模式切换后重新适配视图;须等节点尺寸就绪,否则 fitView 漏算未测量节点,视角偏向已测量的 ClusterNode */
  function refitView() {
    if (!nodes.value.length) return
    if (nodesInitialized.value) {
      fitView({ padding: 0.2, duration: 300 })
      return
    }
    // 节点尚未完成尺寸测量:标记待处理,由上方 watch 在 nodesInitialized 翻 true 后统一适配
    pendingFit += 1
  }

  /** 全量拓扑回填:首拉与轮询共用;空响应视为瞬态异常保留旧图;当前集群被移除(下线/删除)时回退首个可用集群 */
  function applyTopologies(list: ClusterGraphData[]) {
    // 空列表(后端瞬态异常/部分成功)不回填:否则画布被清空、集群回退 0、下轮数据回来再 fitView,等于重置视图
    if (!list.length) return
    graphMap.value = new Map(list.map((item) => [item.cluster.id, item]))
    if (!graphMap.value.has(currentClusterId.value)) {
      currentClusterId.value = [...graphMap.value.keys()][0] ?? 0
    }
  }

  /**
   * 轮询实时刷新:首拉完成后 setTimeout 链每 POLL_INTERVAL_MS 拉一轮(链式而非 setInterval,
   * 天然杜绝请求重叠);失败保留旧图(HTTP 层统一提示);页面不可见暂停,恢复可见立即补刷一轮;
   * 卸载 abort 在途请求并停止链条。
   */
  const abortController = new AbortController()
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  function clearPollTimer() {
    if (pollTimer !== null) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
  }
  function scheduleNextPoll() {
    // 已卸载(abort 后 reject 的 tick)不再续链,防止定时器无限自续
    if (abortController.signal.aborted) return
    clearPollTimer()
    if (document.hidden) return
    pollTimer = setTimeout(tickPoll, POLL_INTERVAL_MS)
  }
  async function tickPoll() {
    try {
      applyTopologies(await fetchTopologies(abortController.signal))
    } catch {
      // 轮询失败保留旧图,下一轮继续
    }
    scheduleNextPoll()
  }
  function onVisibilityChange() {
    if (document.hidden) clearPollTimer()
    else void tickPoll()
  }

  /** 拉取全部集群拓扑;初始集群 id 优先,名称兜底,都没有则用第一个;完成后启动轮询 */
  onMounted(async () => {
    try {
      const list = await fetchTopologies(abortController.signal)
      applyTopologies(list)
      const byId =
        options.clusterId && graphMap.value.has(options.clusterId) ? options.clusterId : undefined
      const byName = list.find((item) => item.cluster.name === options.clusterName)?.cluster.id
      const initialId = byId ?? byName ?? list[0]?.cluster.id ?? 0
      // 与初始值相同不会触发 watch,统一在此适配视图;等 watchEffect 重建 nodes 后再适配,
      // 否则 nodes 仍为空会被 refitView 跳过
      currentClusterId.value = initialId
      await nextTick()
      refitView()
    } finally {
      loading.value = false
      document.addEventListener('visibilitychange', onVisibilityChange)
      scheduleNextPoll()
    }
  })

  onBeforeUnmount(() => {
    abortController.abort()
    clearPollTimer()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  /** 集群/模式变化 → 重建图 */
  watchEffect(() => {
    const data = graphMap.value.get(currentClusterId.value)
    if (!data) {
      nodes.value = []
      edges.value = []
      return
    }
    const graph = buildGraph(data, mode.value)
    nodes.value = graph.nodes
    edges.value = graph.edges
  })

  /** 详情面板:选中态存节点 id(轮询重建后 id 稳定),record 由 nodes 实时取最新 → 面板随轮询自动刷新;节点已不在画布时自动收起 */
  const selection = computed<PanelSelection | null>(() => {
    const nodeId = selectedNodeId.value
    if (!nodeId) return null
    const node = nodes.value.find((item) => item.id === nodeId)
    return node ? { type: node.type as GraphNodeType, record: node.data.record } : null
  })

  /** 悬停节点时:关联连线加 edge-hot 高亮,其余加 edge-dim 淡出 */
  const displayEdges = computed<Edge[]>(() => {
    // 悬停节点已被轮询刷新移除时视为无悬停,避免全图持续淡出
    const hover = hoverNodeId.value
    if (!hover || !nodes.value.some((item) => item.id === hover)) return edges.value
    return edges.value.map((edgeItem) => ({
      ...edgeItem,
      class: `${edgeItem.class ?? ''} ${
        edgeItem.source === hover || edgeItem.target === hover ? 'edge-hot' : 'edge-dim'
      }`,
    }))
  })

  /** 聚合块点击 → 下钻详细模式;实体节点点击 → 打开详情面板 */
  onNodeClick(({ node }) => {
    if (node.type === 'aggregate') {
      mode.value = 'detail'
      return
    }
    selectedNodeId.value = node.id
  })
  onPaneClick(() => {
    selectedNodeId.value = ''
  })
  onNodeMouseEnter(({ node }) => {
    hoverNodeId.value = node.id
  })
  onNodeMouseLeave(() => {
    hoverNodeId.value = ''
  })

  /** 集群/模式切换后重新适配视图 */
  watch([currentClusterId, mode], () => {
    // 图重建后旧选中/悬停已不在画布,一并重置
    selectedNodeId.value = ''
    hoverNodeId.value = ''
    refitView()
  })

  /** ESC 关闭详情面板 */
  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') selectedNodeId.value = ''
  }
  window.addEventListener('keydown', onKeydown)
  onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

  /** 各层「前往管理页」跳转映射;aggregate 无管理页,engine 无独立页(跳部署页);
   *  GPU 页已下线(GPU 信息并入节点列表:行展开清单 + GPU 列弹窗),跳节点列表 */
  const JUMP_ROUTES: Partial<Record<GraphNodeType, string>> = {
    node: 'node-list',
    gpu: 'node-list',
    deploy: 'deploy',
    engine: 'deploy',
    model: 'model-management',
  }
  function jumpToManage() {
    if (!selection.value) return
    // 集群的管理页就是当前所在的 node-pool:直接退出拓扑回表格
    if (selection.value.type === 'cluster') {
      onExit()
      return
    }
    const target = JUMP_ROUTES[selection.value.type]
    if (target) void router.push({ name: target })
  }
  function closePanel() {
    selectedNodeId.value = ''
  }

  const clusterOptions = computed(() =>
    [...graphMap.value.values()].map((item) => ({ label: item.cluster.name, value: item.cluster.id })),
  )
  /** 加载完成但无任何 active 集群拓扑 */
  const isEmpty = computed(() => !loading.value && graphMap.value.size === 0)
  const modeOptions = [
    { label: '简洁', value: 'compact' },
    { label: '详细', value: 'detail' },
  ]

  return {
    clusterOptions,
    currentClusterId,
    mode,
    modeOptions,
    nodes,
    displayEdges,
    selection,
    loading,
    isEmpty,
    closePanel,
    jumpToManage,
  }
}
