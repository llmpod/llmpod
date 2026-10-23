import { useConfirm, useDialog, message, copyToClipboard } from '@repo/hooks'
import type { Edge, Node } from '@vue-flow/core'
import { useNodesInitialized, useVueFlow } from '@vue-flow/core'
import { normalizedTopology, toReplicaSeq, type ModelEndpoint } from '@/api/deployment'
import {
  DEPLOY_CREATE_PHASES,
  DEPLOY_PHASE_LABEL,
  deployStatusMap,
  useStore as useListStore,
  type DeployRecord,
} from '../useStore'
import { startDeploymentFlow } from '../startFlow'
import ScaleDialog from './components/ScaleDialog.vue'
import CommandDialog from './components/CommandDialog.vue'
import {
  buildDeployGraph,
  HEALTH_STATUS_TEXT,
  type DeployNodeType,
  type LogEntry,
  type PanelSelection,
} from './graph'
import ContainerConsole from './logs/ContainerConsole.vue'
import LogTimeline from './logs/LogTimeline.vue'
import { pollIntervalMs, useStore } from './useStore'

/**
 * 部署详情(拓扑视图)装配层:
 * 数据源为 GET /{id}(拓扑骨架) + GET /{id}/logs/nodes(节点徽标),
 * 轮询驱动整个视图(进行中 3s、终态 10s、页面不可见暂停,文档 §8);
 * 图结构由 buildDeployGraph 纯函数组装,画布交互对齐 node-pool/topology:
 * 点击节点在画布右上角弹出详情面板(拓扑属性/GPU 节点属性等低频数据藏于面板)。
 */
export function useController(record: DeployRecord, onExit: () => void) {
  const { detail, nodeSummary, topology, loading, load, nodeAssets, assetsLoading, loadNodeAssets } =
    useStore(record.deployment_id)
  const { modelName, stop, remove, subscribe } = useListStore()

  // 订阅列表 store 写操作变更(详情抽屉等外部入口的停止/启动经 notify 联动):
  // 立即手动刷一轮同步拓扑视图状态,不等下一轮轮询
  const unsubscribe = subscribe(() => void load())
  onBeforeUnmount(unsubscribe)

  /** 部署记录:详情返回后用最新数据(轮询/停止后状态会推进),否则用列表行快照 */
  const deployment = computed(() => detail.value?.deployment ?? record)

  /** 状态标签配置(launching 等历史值按 creating 渲染;未知值原样展示) */
  const statusConf = computed(() => {
    const raw = deployment.value.status as string
    const status = raw === 'launching' ? 'creating' : raw
    return (
      deployStatusMap[status as keyof typeof deployStatusMap] ?? {
        label: raw,
        type: 'default' as const,
      }
    )
  })

  /** creating 期间的编排子态文案 */
  const phaseLabel = computed(
    () => DEPLOY_PHASE_LABEL[deployment.value.phase] ?? deployment.value.phase,
  )

  /** 顶部固定进度条:当前步骤序号(1 起,对齐 DEPLOY_CREATE_PHASES;未知子态为 0,不高亮) */
  const phaseCurrent = computed(() => DEPLOY_CREATE_PHASES.indexOf(deployment.value.phase) + 1)

  /** 可停止状态(前端口径;failed 已是停止终态,不再提供停止入口) */
  const stoppable = computed(() => ['running', 'creating'].includes(deployment.value.status))

  /** 可启动状态(后端 start 仅允许 stopped/failed) */
  const startable = computed(() => ['failed', 'stopped'].includes(deployment.value.status))

  /** 可删除状态(删除中隐藏,与列表行内口径一致) */
  const deletable = computed(() => deployment.value.status !== 'deleting')

  /** 推理端点全集(多副本每份一条;后端恒给数组,空数组=未建端点) */
  const endpoints = computed<ModelEndpoint[]>(() => {
    return detail.value?.endpoints ?? []
  })

  /** 端点 → 所属副本序号(replicas[].endpoint_ids 反查;旧数据无 id 映射为 null);返回 1 基展示序号(后端 seq 为 0 基) */
  function endpointSeqOf(endpoint: ModelEndpoint): number | null {
    const id = endpoint.id
    if (id == null) return null
    const replica = (detail.value?.replicas ?? []).find((item) => item.endpoint_ids?.includes(id))
    return replica ? toReplicaSeq(replica.seq) : null
  }

  /** 当前展示的端点:多端点时经下拉切换(默认第一份,列表收缩时回位首项) */
  const activeEndpointIndex = ref(0)
  watch(endpoints, (list) => {
    if (activeEndpointIndex.value >= list.length) activeEndpointIndex.value = 0
  })
  const endpoint = computed(
    () => endpoints.value[activeEndpointIndex.value] ?? endpoints.value[0] ?? null,
  )

  /** 多端点下拉选项(n-select):label = 副本N · 健康态(无副本归属时回退裸健康文案) */
  const endpointOptions = computed(() =>
    endpoints.value.map((item, index) => {
      const seq = endpointSeqOf(item)
      const healthLabel =
        HEALTH_STATUS_TEXT[item.health_status]?.label ?? item.health_status ?? '--'
      return {
        label: `${seq != null ? `副本${seq} · ` : ''}${healthLabel}`,
        value: index,
      }
    }),
  )
  function selectEndpoint(index: number) {
    activeEndpointIndex.value = index
  }

  /** 端点完整 URL(api_base 与 path 之间保证恰好一个 "/",兼容任一侧缺失斜杠) */
  const endpointUrl = computed(() => {
    const ep = endpoint.value
    if (!ep) return ''
    const base = ep.api_base.replace(/\/+$/, '')
    const path = (ep.path ?? '').replace(/^\/+/, '')
    return path ? `${base}/${path}` : base
  })

  /** 在线扩缩容:仅 running 展示入口;SCALING 子态禁用防重入(后端另有 409 兜底) */
  const scalable = computed(() => deployment.value.status === 'running')
  const scaling = computed(() => deployment.value.phase === 'SCALING')

  function openScaleDialog() {
    useDialog({
      title: `调整副本 · ${deployment.value.name}`,
      component: ScaleDialog,
      area: 56,
      btn: false,
      props: {
        deploymentId: deployment.value.deployment_id,
        deploymentName: deployment.value.name,
        currentCopies: normalizedTopology(deployment.value.spec?.topology).copies,
        // 缩容 LIFO 预览按序号排序展示,统一传 1 基人读序号(后端 seq 为 0 基)
        replicaSeqs: (detail.value?.replicas ?? []).map((replica) => toReplicaSeq(replica.seq)),
        hostnameOf: Object.fromEntries(
          (topology.value?.nodes ?? []).map((node) => [node.id, node.hostname] as const),
        ),
        // 提交成功后立即手动刷一轮:状态徽标转 SCALING,轮询自动降为 3s 跟踪
        onScaled: () => void load(),
      },
    })
  }

  /** 复制成功后短暂显示对勾的字段标识(endpoint / servedModel) */
  const copiedKey = ref('')
  let copyTimer: ReturnType<typeof setTimeout> | undefined

  /** 复制文本到剪贴板 */
  async function copyText(text: string, key: string) {
    try {
      await copyToClipboard(text)
      copiedKey.value = key
      clearTimeout(copyTimer)
      copyTimer = setTimeout(() => {
        copiedKey.value = ''
      }, 1500)
    } catch {
      message.error('复制失败,请手动复制')
    }
  }

  /** 详情抽屉:头部「详情」按钮展开(基本信息/端点/实例分区,复用列表抽屉组件) */
  const detailDrawerShow = ref(false)
  function openDetailDrawer() {
    detailDrawerShow.value = true
  }

  /** 实例列表(日志区「容器 stdout」Tab 选项来源;轮询随详情刷新) */
  const instances = computed(() => detail.value?.instances ?? [])

  /** 首次加载中(详情未返回过):画布覆盖 spin;之后轮询静默更新不打断画布 */
  const initialLoading = computed(() => loading.value && !detail.value)

  // 图装配:详情/节点摘要/集群拓扑变化 → 重建节点与连线(id 稳定,轮询重建不影响画布视角)
  const nodes = shallowRef<Node[]>([])
  const edges = shallowRef<Edge[]>([])
  watchEffect(() => {
    // 组装容错:后端出现未知状态/字段缺失等意外形状时保留上一帧图(拓扑静默冻结优于整图消失)
    try {
      const graph = buildDeployGraph({
        deployment: deployment.value,
        instances: detail.value?.instances ?? [],
        nodeSummary: nodeSummary.value,
        modelName: modelName(deployment.value.model_id),
        topology: topology.value,
        replicas: detail.value?.replicas,
      })
      nodes.value = graph.nodes
      edges.value = graph.edges
    } catch (error) {
      console.error('[deploy-detail] 拓扑图组装失败,保留上一帧', error)
    }
  })

  // 视图适配:首次完整图渲染后 fitView 一次;GPU 列数据(topology)异步到达后追加在实例列右侧,
  // 初始 fit 的视口未覆盖,GPU 首次出现时需再 fit 一次,否则新列落在可视区外。
  // 两个就绪条件(完整图已返回 / 节点尺寸已测量)各自独立到达,时序不保证:
  // - 完整图可能先到,但此时节点还是新数组、dimensions 未回填,直接 fit 会漏算;
  // - 残缺图阶段 nodesInitialized 曾翻 true,用「等待 false→true 边沿」会错过详情先到、测量后到的场景。
  // 故用「每帧复查」替代边沿触发:完整图就绪后进入待 fit 状态,
  // 在下一帧及后续帧检查节点尺寸是否回填完毕,满足后立即 fit 并退出状态。
  // 轮询重建时 initialFitted/gpuFitted 已为 true,不会重复 fit。
  const { fitView, onNodeClick, onPaneClick } = useVueFlow()
  const nodesInitialized = useNodesInitialized()
  let pendingFit = false
  let rafId: number | null = null
  /** 待 fit 期间丢失响应式 tracked 节点(组件提前卸载)时退出轮询 */
  let fitStopped = false
  /** 首次完整图已 fit 过(轮询重建不再触发) */
  let initialFitted = false
  /** 含 GPU 列的图已 fit 过(异步 topology 到达后 GPU 首次出现再 fit 一次) */
  let gpuFitted = false

  function scheduleFit() {
    if (fitStopped || rafId != null) return
    rafId = requestAnimationFrame(() => {
      rafId = null
      if (!pendingFit || fitStopped) return
      if (nodesInitialized.value) {
        pendingFit = false
        fitView({ padding: 0.2, duration: 300 })
      } else {
        scheduleFit()
      }
    })
  }

  // 首个详情返回且重建出非空图 → 进入待 fit 状态;GPU 首次出现 → 再 fit 一次
  watch([nodes, initialLoading], ([graphNodes, firstLoading]) => {
    if (firstLoading || pendingFit) return
    if (!graphNodes.length) return
    const hasGpu = graphNodes.some((node) => node.type === 'gpu')
    if (!initialFitted) {
      initialFitted = true
      if (hasGpu) gpuFitted = true
      pendingFit = true
      scheduleFit()
    } else if (hasGpu && !gpuFitted) {
      gpuFitted = true
      pendingFit = true
      scheduleFit()
    }
  })

  onBeforeUnmount(() => {
    fitStopped = true
    if (rafId != null) cancelAnimationFrame(rafId)
  })

  // 详情面板:选中态存节点 id(轮询重建后 id 稳定),record 实时取最新 → 面板数据随轮询自动刷新;
  // 选中节点已不在画布(实例被回收)时面板自动收起
  const selection = ref<string | null>(null)
  const panelSelection = computed<PanelSelection | null>(() => {
    const selectedId = selection.value
    if (!selectedId) return null
    const node = nodes.value.find((item) => item.id === selectedId)
    return node ? { type: node.type as DeployNodeType, record: node.data.record } : null
  })
  function closePanel() {
    selection.value = null
  }

  /**
   * 日志弹窗(各层节点卡片「日志」按钮入口,按 entry 分流):
   * 部署/物理节点→时间线(结构化历史,卸载随弹窗关闭停止);实例→容器 stdout(tail-log 常连接,卸载自动 abort)。
   * props 为打开时快照,弹窗存续期间轮询变化不回灌——关闭重开即最新。
   */
  function openLogDialog(entry: LogEntry) {
    const deploy = deployment.value
    if (entry.type === 'console') {
      useDialog({
        title: `容器日志 · ${deploy.name}`,
        component: ContainerConsole,
        area: 90,
        btn: false,
        props: {
          deploymentId: deploy.deployment_id,
          instances: instances.value,
          initialInstanceId: entry.instanceId,
        },
      })
      return
    }
    useDialog({
      title: entry.nodeId ? `节点日志 · ${entry.nodeId.slice(0, 8)}` : `编排日志 · ${deploy.name}`,
      component: LogTimeline,
      area: 90,
      btn: false,
      props: {
        deploymentId: deploy.deployment_id,
        status: deploy.status,
        nodeId: entry.nodeId ?? null,
      },
    })
  }

  /**
   * 启动命令弹窗(实例详情面板「启动命令」):该容器下发那一刻的完整 docker run。
   * 命令不可变,打开拉一次;props 为打开时快照,弹窗存续期间不回灌——关闭重开即最新。
   */
  function openCommandDialog(instanceId: string) {
    const deploy = deployment.value
    const inst = instances.value.find((item) => item.instance_id === instanceId)
    const label = inst ? `#${inst.rank} ${inst.role}` : instanceId
    useDialog({
      title: `启动命令 · ${deploy.name} ${label}`,
      component: CommandDialog,
      area: 62,
      btn: false,
      props: {
        deploymentId: deploy.deployment_id,
        instanceId,
      },
    })
  }
  onNodeClick(({ node }) => {
    selection.value = node.id
    // 物理节点详情需要集群拓扑资产(主机名/IP/GPU):点击时按需拉取
    if (node.type === 'phy') void loadNodeAssets(deployment.value.cluster_id)
  })
  onPaneClick(() => {
    selection.value = null
  })
  // ESC 关闭详情面板
  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') selection.value = null
  }
  window.addEventListener('keydown', onKeydown)
  onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

  // 轮询:setTimeout 链按最新状态取间隔(到终态下一轮自动降频);不可见时暂停,恢复可见立即补刷一轮;
  // disposed 守卫:卸载后不再续链(在途请求的 continuation 不得重新挂表,防止定时器无限自续泄漏)
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | null = null
  function clearTimer() {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }
  function scheduleNext() {
    if (disposed) return
    clearTimer()
    if (document.hidden) return
    // SCALING 是 phase 子态(status 仍 running),需一并传入按 3s 密轮询
    timer = setTimeout(tick, pollIntervalMs(deployment.value.status, deployment.value.phase))
  }
  async function tick() {
    await load()
    if (disposed) return
    // 物理节点详情面板打开期间:随轮询刷新资产(GPU 利用率/可用显存是实时值)
    if (panelSelection.value?.type === 'phy') {
      void loadNodeAssets(deployment.value.cluster_id)
    }
    scheduleNext()
  }
  function onVisibilityChange() {
    if (document.hidden) clearTimer()
    else void tick()
  }

  onMounted(() => {
    void tick()
    document.addEventListener('visibilitychange', onVisibilityChange)
  })
  onBeforeUnmount(() => {
    disposed = true
    clearTimer()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  /** 停止:确认后调接口,成功立即手动刷一轮(不等轮询,文档 §5);列表经 notify 联动刷新 */
  async function onStop() {
    const data = deployment.value
    try {
      await useConfirm({
        title: '停止部署',
        content: `确定停止部署「${data.name}」吗?停止后推理端点将不可用。`,
        confirmText: '停止',
        cancelText: '取消',
      })
      await stop(data.deployment_id)
      message.success(`已停止:${data.name}`)
      void load()
    } catch {
      // 取消或接口异常(HTTP 层已统一提示),流程已打断
    }
  }

  /** 启动:共享确认/强制启动流(列表同一套),成功立即手动刷一轮(不等轮询);列表经 notify 联动刷新 */
  async function onStart() {
    if (await startDeploymentFlow(deployment.value)) void load()
  }

  /** 删除:确认后调接口,成功退出详情视图;列表经 notify 联动刷新 */
  async function onRemove() {
    const data = deployment.value
    try {
      await useConfirm({
        title: '删除部署',
        content: `确定删除部署「${data.name}」吗?将停止容器并删除实例与端点,不可恢复。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await remove(data.deployment_id)
      message.success(`已删除:${data.name}`)
      onExit()
    } catch {
      // 取消或接口异常(HTTP 层已统一提示),流程已打断
    }
  }

  return {
    deployment,
    statusConf,
    phaseLabel,
    phaseCurrent,
    stoppable,
    startable,
    deletable,
    endpoints,
    endpoint,
    endpointOptions,
    endpointUrl,
    activeEndpointIndex,
    selectEndpoint,
    scalable,
    scaling,
    openScaleDialog,
    copiedKey,
    copyText,
    detailDrawerShow,
    openDetailDrawer,
    instances,
    nodes,
    edges,
    initialLoading,
    panelSelection,
    closePanel,
    openLogDialog,
    openCommandDialog,
    nodeAssets,
    assetsLoading,
    onStart,
    onStop,
    onRemove,
    back: onExit,
  }
}
