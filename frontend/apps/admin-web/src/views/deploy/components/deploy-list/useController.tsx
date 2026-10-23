import { NTag, NTooltip } from 'naive-ui'
import { useRoute, useRouter } from 'vue-router'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmBatchAction, LmSelectAction, LmTableBatchSelectPayload } from '@repo/naive-ui'
import {
  useBatch,
  useConfirm,
  useDialog,
  useEntryAction,
  useOperate,
  useTable,
  message,
} from '@repo/hooks'
import { getClusterTopology } from '@/api/topology'
import { normalizedTopology, toReplicaSeq } from '@/api/deployment'
import { startDeploymentFlow } from './startFlow'
import ScaleDialog from './deploy-detail/components/ScaleDialog.vue'
import {
  DEPLOY_STATUS_OPTIONS,
  deployStatusMap,
  formatDateTime,
  gpuSummary,
  useStore,
  type DeployRecord,
} from './useStore'

/** 可停止状态(前端口径;failed 已是停止终态,不再提供停止入口) */
const STOPPABLE_STATUS = ['running', 'creating']

/** 可启动状态(后端 start 仅允许 stopped/failed) */
const STARTABLE_STATUS = ['failed', 'stopped']

export function useController() {
  const { request, modelName, loadModelNames, fetchDetail, stop, remove, removeMany, subscribe } =
    useStore()

  /** 状态筛选选项 */
  const statusOptions = DEPLOY_STATUS_OPTIONS

  /**
   * 新建部署:内嵌三步向导覆盖表格区域(基本信息 → 选择算力 → 确认部署)。
   * initialModelId:入口预选模型(模型管理行操作「新建部署」跳入),向导打开即回填表单。
   */
  const wizardOpen = ref(false)
  const wizardInitialModelId = ref<number | null>(null)
  function openCreate(initialModelId: number | null = null) {
    wizardInitialModelId.value = initialModelId
    wizardOpen.value = true
  }
  /**
   * 向导退出:确认部署成功时携带刚创建的部署记录,关闭向导后直接打开该部署的详情视图
   * (不回列表);取消/关闭向导时不带参,回到列表。
   */
  function onWizardExit(created?: DeployRecord) {
    wizardOpen.value = false
    if (created) openDetailView(created)
  }

  /** 部署向导:跳转一键部署向导页 */
  const router = useRouter()
  const route = useRoute()
  function openWizard() {
    router.push({ name: 'quick-setup' })
  }

  /** 部署详情:整页拓扑视图覆盖表格区域(与新建向导同一层),退出回到表格;详情抽屉入口在视图头部 */
  const detailViewRecord = ref<DeployRecord | null>(null)

  function openDetailView(row: DeployRecord) {
    detailViewRecord.value = row
  }
  function closeDetailView() {
    detailViewRecord.value = null
  }

  /** 入口详情待定:?action=detail 进入时详情接口在途,以骨架占位替代列表,避免先闪列表再切详情 */
  const detailEntryPending = ref(false)

  /** 入口动作「查看部署详情」:?action=detail&id=<deployment_id>(一键部署完成页/运营大屏跳转),拉取详情后直接进入拓扑视图 */
  async function openDetailEntry() {
    const id = route.query.id
    if (typeof id !== 'string' || !id) return
    detailEntryPending.value = true
    try {
      const detail = await fetchDetail(id)
      openDetailView(detail.deployment)
    } catch {
      // 详情接口异常由 HTTP 层统一提示,停留在列表
    } finally {
      detailEntryPending.value = false
    }
  }

  /**
   * 入口动作「按状态筛选」:?action=status&status=running(运营大屏部署概览跳转),校验合法状态后合并筛选。
   * 初始进入(useEntryAction 的 immediate watch 在 setup 期触发,先于表格 onMounted 首刷):
   * 只写参数不请求,由表格 immediate 首刷统一带出,避免同参数列表接口连发两次;
   * 已停留在本页(全局搜索操作落地,组件不重挂载)时首刷已过,需 search 主动刷新。
   */
  let tableMounted = false
  onMounted(() => {
    tableMounted = true
  })

  function applyStatusEntry() {
    const status = route.query.status
    if (
      typeof status === 'string' &&
      status !== '' &&
      DEPLOY_STATUS_OPTIONS.some((option) => option.value === status)
    ) {
      if (tableMounted) void search({ status })
      else setParam({ status })
    }
  }

  /** 停止:二次确认后调用接口,成功后由 store notify 联动表格刷新 */
  async function onStop(row: DeployRecord) {
    try {
      await useConfirm({
        title: '停止部署',
        content: `确定停止部署「${row.name}」吗?停止后推理端点将不可用。`,
        confirmText: '停止',
        cancelText: '取消',
      })
      await stop(row.deployment_id)
      message.success(`已停止:${row.name}`)
    } catch {
      // 取消或接口异常(HTTP 层已统一提示),流程已打断
    }
  }

  /** 删除:二次确认后调用接口(标 deleting 级联清理),成功后由 store notify 联动表格刷新 */
  async function onRemove(row: DeployRecord) {
    try {
      await useConfirm({
        title: '删除部署',
        content: `确定删除部署「${row.name}」吗?将停止容器并删除实例与端点,不可恢复。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await remove(row.deployment_id)
      message.success(`已删除:${row.name}`)
    } catch {
      // 取消或接口异常(HTTP 层已统一提示),流程已打断
    }
  }

  /** 行操作:部署详情(拓扑视图) / 启动(失败/已停止) / 停止(运行中/部署中) / 删除(删除中隐藏) */
  // 启动/停止/删除的 onClick 返回 Promise:慢接口期间按钮由 LmTableSelect 自动转 loading 并防连点
  const selectActions: LmSelectAction<DeployRecord>[] = [
    {
      title: '启动',
      show: (row) => STARTABLE_STATUS.includes(row.status),
      onClick: (row) => startDeploymentFlow(row),
    },
    {
      title: '停止',
      show: (row) => STOPPABLE_STATUS.includes(row.status),
      onClick: onStop,
    },
    { title: '部署详情', onClick: openDetailView },
    { title: '删除', danger: true, show: (row) => row.status !== 'deleting', onClick: onRemove },
  ]

  /** 批量操作:批量删除(二次确认后逐个标记删除,store notify 联动刷新,LmBatch 自动清空选择) */
  const batchActions: LmBatchAction[] = [{ label: '批量删除', value: 'delete', danger: true }]

  async function handleBatch(payload: LmTableBatchSelectPayload) {
    if (payload.action !== 'delete') return
    try {
      await useConfirm({
        title: '批量删除',
        content: `确定删除选中的 ${payload.selectedRows.length} 个部署吗?将停止容器并删除实例与端点,不可恢复。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await removeMany(payload.selectedKeys as string[])
      message.success(`已删除 ${payload.selectedRows.length} 个部署`)
    } catch {
      // 取消或接口异常(HTTP 层已统一提示),流程已打断
    }
  }

  /**
   * 在线扩缩容(列表「副本数」列入口):先拉详情/集群拓扑取副本序号(LIFO 预览)与
   * hostname 映射(扩容落点可读化),复用详情页 ScaleDialog;提交成功后刷新列表。
   */
  let scaleDialogOpening = false
  async function openScaleDialog(row: DeployRecord) {
    if (scaleDialogOpening) return
    scaleDialogOpening = true
    try {
      const [detail, topology] = await Promise.all([
        fetchDetail(row.deployment_id),
        getClusterTopology(row.cluster_id).catch(() => null),
      ])
      useDialog({
        title: `调整副本 · ${row.name}`,
        component: ScaleDialog,
        area: 56,
        btn: false,
        props: {
          deploymentId: row.deployment_id,
          deploymentName: row.name,
          currentCopies: normalizedTopology(row.spec?.topology).copies,
          // 缩容 LIFO 预览统一传 1 基人读序号(后端 seq 为 0 基)
          replicaSeqs: (detail.replicas ?? []).map((replica) => toReplicaSeq(replica.seq)),
          hostnameOf: Object.fromEntries(
            (topology?.nodes ?? []).map((node) => [node.id, node.hostname] as const),
          ),
          // 提交成功后立即刷一轮:副本数变更/SCALING 徽标随轮询推进
          onScaled: () => void refresh(),
        },
      })
    } catch {
      // 详情接口异常由 HTTP 层统一提示,停留在列表
    } finally {
      scaleDialogOpening = false
    }
  }

  const { LmTable, LmPage, LmBatch, param, search, setParam, hasActiveQuery, reset, refresh, data, loading } =
    useTable<DeployRecord, { status: string }>({
      request,
      param: { status: '' },
      columns: [
        // useCheckbox 参数会被丢弃且无法禁用行,这里手写选择列:删除中的行不可勾选(与行内删除入口一致)
        {
          type: 'selection',
          multiple: true,
          disabled: (row) => row.status === 'deleting',
        },
        {
          title: '部署名',
          key: 'name',
          minWidth: 140,
          render: (row) =>
            h(
              'span',
              {
                class: 'text-primary cursor-pointer hover:opacity-[0.9]',
                onClick: () => openDetailView(row),
              },
              row.name,
            ),
        },
        {
          title: '模型',
          key: 'model_id',
          minWidth: 140,
          render: (row) => modelName(row.model_id),
        },
        {
          title: '引擎',
          key: 'engine',
          width: 100,
          render: (row) =>
            h(
              NTag,
              {
                size: 'small',
                bordered: false,
                color: {
                  color: 'var(--c-tag-purple)',
                  textColor: 'rgb(var(--n-primary-color))',
                },
              },
              { default: () => row.engine },
            ),
        },
        {
          title: '状态',
          key: 'status',
          width: 140,
          render: (row) => {
            const conf = deployStatusMap[row.status] ?? { label: row.status, type: 'default' }
            return h('div', { class: 'flex flex-col gap-[.2rem] leading-tight' }, [
              h(
                NTag,
                { type: conf.type, size: 'small', bordered: false, class: 'self-start' },
                { default: () => conf.label },
              ),
            ])
          },
        },
        {
          title: '算力',
          key: 'gpu',
          width: 130,
          render: (row) => {
            const summary = gpuSummary(row)
            if (summary === '--') return '--'
            // 纯 CPU 部署无卡,tooltip 不再宣称「总卡数 = 卡数 × 副本数」
            const tip = summary.startsWith('纯 CPU')
              ? '纯 CPU 部署,不占用 GPU'
              : '单副本规格;总卡数 = 卡数 × 副本数,多副本可能共用节点'
            return h(
              NTooltip,
              { trigger: 'hover' },
              {
                trigger: () => h('span', summary),
                default: () => tip,
              },
            )
          },
        },
        {
          title: '副本数',
          key: 'copies',
          width: 100,
          render: (row) => {
            const copies = normalizedTopology(row.spec?.topology).copies
            // 扩缩进行中(phase 子态,后端 409 防重入):徽标替代数字,不可再调
            if (row.status === 'running' && row.phase === 'SCALING') {
              return h(
                NTag,
                { size: 'small', type: 'warning', bordered: false },
                { default: () => '扩缩容中' },
              )
            }
            // 仅 running 提供在线扩缩入口(与后端 scale 约束一致),其余状态纯展示
            if (row.status !== 'running') return `${copies} 份`
            return h(
              NTooltip,
              { trigger: 'hover' },
              {
                trigger: () =>
                  h(
                    'span',
                    {
                      class: 'text-primary cursor-pointer hover:opacity-[0.9]',
                      onClick: () => void openScaleDialog(row),
                    },
                    `${copies} 份`,
                  ),
                default: () => '点击调整副本数(在线扩缩容)',
              },
            )
          },
        },
        {
          title: '创建时间',
          key: 'created_at',
          width: 130,
          render: (row) => formatDateTime(row.created_at),
        },
        useOperate<DeployRecord>(selectActions, { width: 140 }),
      ],
      rowKey: 'deployment_id',
      extension: [useBatch(batchActions)],
      config: {
        pagination: { pageSize: 10 },
        request: { pageSizeField: 'page_size' },
        tableProps: { flexHeight: false },
      },
    })

  /** 首次部署引导:默认不展示(含加载中),表格请求返回后按「无数据且无生效筛选」(真正的首次使用)判断 */
  const tableLoaded = ref(false)
  watch(loading, (value, previous) => {
    if (previous) tableLoaded.value = true
  })
  const showFirstDeployAlert = computed(
    () => tableLoaded.value && data.value.length === 0 && !hasActiveQuery.value,
  )

  /** 状态筛选变更:合并查询参数并回到第 1 页 */
  function handleStatusChange(value: string) {
    void search({ status: value })
  }

  /** 入口动作「新建部署」:?action=create 或 ?action=deploy&model=<模型主键>(后者为模型管理行操作跳入),解析预选模型后打开向导 */
  function applyCreateEntry() {
    const raw = route.query.model
    const modelId = typeof raw === 'string' && raw !== '' && Number.isFinite(Number(raw)) ? Number(raw) : null
    openCreate(modelId)
  }

  // 全局搜索入口:?action=create 打开新建向导,?action=wizard 进入部署向导;
  // ?action=detail&id= 直达指定部署详情(一键部署完成页「查看部署详情」);
  // ?action=status&status= 按状态自动筛选(运营大屏部署概览);一次性参数随 action 一并清除。
  // ?action=deploy&model=<id>:模型管理「新建部署」跳入——独立键因模型管理内嵌在 deploy 路由
  // 的 management 子 tab,同路由切 tab 时其 useEntryAction({create}) 会先于卸载看到同名 action
  // 并误开「添加模型」弹窗、清掉 query;独立键在其 handlers 中不存在,天然互不干扰
  useEntryAction({
    create: { handler: applyCreateEntry, clear: ['model'] },
    deploy: { handler: applyCreateEntry, clear: ['model'] },
    wizard: openWizard,
    detail: { handler: openDetailEntry, clear: ['id'] },
    status: { handler: applyStatusEntry, clear: ['status'] },
  })

  // 模型名映射懒加载(列表/详情回显模型名);失败静默,回退 #id 展示
  onMounted(() => {
    void loadModelNames()
  })

  // 新建成功后 store notify → 刷新列表;卸载时取消订阅
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  // 部署异步推进:列表存在 creating/deleting 记录(或 running 下的 SCALING 扩缩容子态)时
  // 每 3s 轮询,全部进入终态后停止。
  // 续期挂在 loading 边沿(每轮请求结束,无论成败)而非 watch(data):refresh 失败时 data 引用
  // 不变会让 watch 不触发,轮询链永久断裂,部署中记录的跟踪随之停止。
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  function clearPollTimer() {
    if (pollTimer) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
  }
  function schedulePoll() {
    clearPollTimer()
    const tracking = data.value.some(
      (row) =>
        row.status === 'creating' ||
        row.status === 'deleting' ||
        // SCALING 是 running 的 phase 子态,收紧判定避免误报(如 failed + SCALING 残留)
        (row.status === 'running' && row.phase === 'SCALING'),
    )
    if (tracking) pollTimer = setTimeout(() => void refresh(), 3000)
  }
  watch(loading, (isLoading, wasLoading) => {
    // 仅在「请求结束」的下降沿续期(首次进入页面 wasLoading 为 true → false 也会触发一次)
    if (wasLoading && !isLoading) schedulePoll()
  })
  onBeforeUnmount(clearPollTimer)

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmBatch,
    statusOptions,
    param,
    detailViewRecord,
    detailEntryPending,
    closeDetailView,
    hasActiveQuery,
    showFirstDeployAlert,
    reset,
    wizardOpen,
    wizardInitialModelId,
    openCreate,
    onWizardExit,
    openWizard,
    handleStatusChange,
    handleBatch,
  }
}
