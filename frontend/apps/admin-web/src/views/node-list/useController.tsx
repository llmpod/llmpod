import { onBeforeUnmount } from 'vue'
import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useConfirm, useEntryAction, message } from '@repo/hooks'
import type { TableRequest } from '@repo/hooks'
import { usePolling } from '@/analytics/usePolling'
import GpuInfoModal from './GpuInfoModal.vue'
import NodeGpuExpand from './NodeGpuExpand.vue'
import NodeRegisterModal from './NodeRegisterModal.vue'
import {
  useStore,
  nodeStatusMap,
  nodeVersionStatusMap,
  gpuSummary,
  formatPercent,
  formatGbPair,
  progressColor,
  type ComputeNode,
  type NodeRuntime,
} from './useStore'

/**
 * 列表自动刷新间隔:节点心跳/版本状态靠轮询带出。
 * 取 10s:心跳本身是分钟级语义,3s 一轮等于把同一份数据每分钟重拉 20 次
 * (每轮 2 个请求 + 一次整表重渲),前后端都在为「看不出变化」付钱。
 */
const LIST_POLL_INTERVAL_MS = 10_000

/**
 * 静默轮询里 GPU 全量补拉的节奏:每 N 轮补一次(10s × 6 = 60s)。
 * 其余轮次只拉 `/admin/nodes`,GPU 复用 store 里的上一次全量结果 ——
 * 主表格的 GPU 型号/显存/驱动与展开行的单卡指标在 60s 内收敛一次,
 * 而 GPU 全量流量从「每轮一次 100 条」降到 1/6。
 */
const GPU_RESYNC_EVERY_POLLS = 6

export function useController() {
  const { request, upgrade, subscribe } = useStore()

  /** 顶部提示图标:public/icons/node-list/alert.svg,mask 渲染跟随 alert 图标配色 */
  const alertIcon = (() => {
    const src = `${import.meta.env.BASE_URL}icons/node-list/alert.svg`
    return {
      backgroundColor: 'currentColor',
      mask: `url(${src}) center/contain no-repeat`,
      WebkitMask: `url(${src}) center/contain no-repeat`,
    }
  })()

  /** 当前操作的节点 + 抽屉显隐 */
  const activeNode = ref<ComputeNode | null>(null)
  const gpuDrawerShow = ref(false)
  const detailDrawerShow = ref(false)

  function openGpuDrawer(row: ComputeNode) {
    activeNode.value = row
    gpuDrawerShow.value = true
  }

  function openDetailDrawer(row: ComputeNode) {
    activeNode.value = row
    detailDrawerShow.value = true
  }

  /** GPU 列点击:GPU 详细信息弹窗 */
  function openGpuInfo(row: ComputeNode) {
    useDialog({
      title: 'GPU详细信息',
      component: GpuInfoModal,
      area: 42,
      props: { node: row },
      btn: { confirmText: '确认', showCancel: false },
    })
  }

  /**
   * 最近一次列表请求的页码/页大小/关键字:静默轮询据此重放,
   * 不走 useTable.refresh —— 它会翻 loading 遮罩,轮一次就闪一次。
   */
  let lastQuery: { page: number, pageSize: number, keyword: string } | null = null

  /**
   * 上一轮已写进表格的数据指纹:轮询结果与它一致就跳过 setData。
   * 整表重渲(每行每列的 render 全部重跑)远比一次字符串比较贵;JSON 比较虽朴素,
   * 但覆盖全部展示字段,不会因为漏比某个指标而把变化吃掉。
   */
  let lastSignature: string | null = null

  function dataSignature(rows: ComputeNode[], total: number): string {
    return JSON.stringify([total, rows])
  }

  /** 包装 store.request:顺带记住当前查询参数供静默轮询复用,其余行为(含 signal 取消)原样透传 */
  const tableRequest: TableRequest<ComputeNode, { keyword: string }> = (params, ctx) => {
    const pageSize = (params as { page_size?: number }).page_size ?? params.pageSize
    lastQuery = { page: params.page, pageSize, keyword: params.keyword ?? '' }
    // 首屏/翻页/手动刷新确立新基线:轮询据此判断「数据是否真的变了」
    return request(params, ctx).then((result) => {
      lastSignature = dataSignature(result.data, result.total)
      return result
    })
  }

  /** 升级下发中的节点ID(行/卡片操作防连点;节点侧本身自判忽略重复,前端只防误触) */
  const upgradingId = ref<string | null>(null)

  /**
   * 下发节点升级:二次确认后调接口。
   * 返回只代表指令送达 —— 没有进度条也不阻塞:完成信号是列表 version_status 翻 ok
   * (镜像预载 <30s,需拉取则分钟级),期间节点 status 全程 online 没有「升级中」态;
   * 节点侧失败(拉取失败/回滚/存量节点不支持)只在审计页($node_upgrade_result),不在此展示。
   */
  async function onUpgrade(row: ComputeNode) {
    if (upgradingId.value !== null) return
    // 确认弹窗展开期间即占位:避免快速连点叠出多个确认框(节点侧本身自判忽略重复,前端只防误触)
    upgradingId.value = row.id
    try {
      await useConfirm({
        title: '升级节点',
        content: `确定将节点「${row.hostname}」升级到与管理端一致的版本吗?不影响已部署的推理服务。`,
        confirmText: '升级',
        cancelText: '取消',
      })
    }
    catch {
      upgradingId.value = null
      return
    }
    try {
      const result = await upgrade(row.id)
      // reason 由后端给出中文原因,直接透出不改写
      if (result.dispatched) message.success(`已下发升级:${row.hostname}`)
      else message.warning(result.reason ?? '升级下发失败')
    }
    finally {
      upgradingId.value = null
    }
  }

  /** 行操作:节点详情抽屉(GPU 详细信息已由表格行展开承接;卡片模式仍走 GPU 抽屉) */
  const selectActions: LmSelectAction<ComputeNode>[] = [
    { title: '详情', onClick: openDetailDrawer },
    {
      title: '升级',
      // 只有落后管理端版本的节点才有升级入口;同版/超前/未上报不显示
      show: (row: ComputeNode) => row.version_status === 'outdated',
      disabled: (row: ComputeNode) => upgradingId.value === row.id,
      onClick: onUpgrade,
    },
  ]

  /**
   * 用量单元格:百分比 + 迷你进度条(悬浮 title 展示用量明细);
   * runtime 为 null(离线)或 disabled(如无 GPU)时渲染 —,不把离线的 0 当真实值
   */
  function renderUsage(
    runtime: NodeRuntime | null,
    percent: number,
    tip?: string,
    disabled = false,
  ) {
    if (!runtime || disabled) return h('span', { class: 'text-secondary' }, '—')
    const width = Math.min(100, Math.max(0, Math.round(percent)))
    const colorClass = progressColor(percent)
    return h('div', { class: 'flex flex-col gap-[.3rem] py-[.3rem]', title: tip }, [
      h('span', formatPercent(percent)),
      h('div', { class: 'h-[.4rem] w-[6rem] rounded-full bg-fill-4 overflow-hidden' }, [
        h('div', { class: `h-full rounded-full ${colorClass}`, style: { width: `${width}%` } }),
      ]),
    ])
  }

  const { LmTable, LmPage, LmRefresh, LmColumn, LmSearch, refresh, hasActiveQuery, reset, data: nodes, loading, setData } = useTable<
    ComputeNode,
    { keyword: string }
  >({
    request: tableRequest,
    param: { keyword: '' },
    columns: [
      // 行展开:原 GPU 管理抽屉内容(驱动信息 + GPU 列表),数据取行聚合 gpus,不再单独请求
      {
        type: 'expand',
        renderExpand: (row: ComputeNode) => h(NodeGpuExpand, { gpus: row.gpus }),
      },
      {
        title: '主机名',
        key: 'hostname',
        minWidth: 140,
        hideable: false,
        render: (row) =>
          h(
            'span',
            { class: 'text-primary cursor-pointer hover:opacity-[0.9]', onClick: () => openDetailDrawer(row) },
            row.hostname,
          ),
      },
      { title: 'IP', key: 'ip', width: 130 },
      {
        title: 'GPU',
        key: 'gpu',
        width: 160,
        render: (row) => {
          const summary = gpuSummary(row.gpus)
          if (!row.gpus.length) return summary.label
          return h(
            'span',
            { class: 'text-primary cursor-pointer hover:opacity-[0.9]', onClick: () => openGpuInfo(row) },
            summary.label,
          )
        },
      },
      {
        title: '显存',
        key: 'vram',
        width: 100,
        render: (row) => gpuSummary(row.gpus).vram,
      },
      {
        title: 'GPU 驱动',
        key: 'driver',
        minWidth: 140,
        render: (row) => {
          const driver = gpuSummary(row.gpus).driver
          return driver === '未安装' ? h('span', { class: 'text-error' }, '未安装') : driver
        },
      },
      {
        title: '版本',
        key: 'version',
        minWidth: 150,
        render: (row) => {
          // 未上报/存量节点:version 为 null → 显示 —;同版只显示版本号,落后/超前附小标签
          if (!row.version) return h('span', { class: 'text-secondary' }, '—')
          if (row.version_status === 'ok') {
            return h('span', { class: 'tabular-nums' }, row.version)
          }
          const conf = nodeVersionStatusMap[row.version_status] ?? nodeVersionStatusMap.unknown
          // 落后节点:标签本身即升级入口(primary 高亮 + 可点),点击等同行操作「升级」;
          // 升级下发中降级为禁用态,避免同一节点重复确认
          const outdated = row.version_status === 'outdated'
          const upgrading = outdated && upgradingId.value === row.id
          return h('div', { class: 'flex items-center gap-[.4rem]' }, [
            h('span', { class: 'tabular-nums' }, row.version),
            h(
              NTag,
              {
                type: outdated ? 'primary' : conf.type,
                size: 'small',
                bordered: false,
                class: outdated ? (upgrading ? 'cursor-not-allowed' : 'cursor-pointer') : undefined,
                title: outdated ? '点击升级到与管理端一致的版本' : undefined,
                role: outdated ? 'button' : undefined,
                'aria-label': outdated ? `升级节点 ${row.hostname}` : undefined,
                onClick: outdated
                  ? () => {
                      if (upgradingId.value !== null) return
                      void onUpgrade(row)
                    }
                  : undefined,
              },
              { default: () => conf.label },
            ),
          ])
        },
      },
      {
        title: '状态',
        key: 'status',
        width: 100,
        render: (row) => {
          const conf = nodeStatusMap[row.status] ?? { label: row.status, type: 'default' }
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
      {
        title: 'CPU',
        key: 'cpu_percent',
        width: 110,
        render: (row) => renderUsage(row.runtime, row.runtime?.cpu_percent ?? 0),
      },
      {
        title: '内存',
        key: 'memory_percent',
        width: 120,
        render: (row) =>
          renderUsage(
            row.runtime,
            row.runtime?.memory_percent ?? 0,
            row.runtime ? `已用 ${formatGbPair(row.runtime.memory_used_gb, row.memory_gb)}` : undefined,
          ),
      },
      {
        title: '磁盘',
        key: 'disk_percent',
        width: 120,
        render: (row) =>
          renderUsage(
            row.runtime,
            row.runtime?.disk_percent ?? 0,
            row.runtime ? formatGbPair(row.runtime.disk_used_gb, row.runtime.disk_total_gb) : undefined,
          ),
      },
      {
        title: 'GPU 负载',
        key: 'gpu_utilization',
        width: 120,
        render: (row) =>
          renderUsage(
            row.runtime,
            row.runtime?.gpu_utilization ?? 0,
            row.runtime ? `显存 ${formatGbPair(row.runtime.used_vram_gb, row.runtime.total_vram_gb)}` : undefined,
            !row.runtime?.gpu_count,
          ),
      },
      useOperate<ComputeNode>(selectActions, { width: 110 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
      request: { pageSizeField: 'page_size' },
      // 窄屏卡片:主机名做主标题,IP/状态/GPU 对齐成列,用量与版本收进展开区;
      // 离线/维护节点才出左侧边轨(正常节点不涂色)
      card: {
        titleKey: 'hostname',
        fieldKeys: ['ip', 'status', 'gpu'],
        alertWhen: (row: ComputeNode) =>
          row.status === 'offline' ? 'error' : row.status === 'maintenance' ? 'warning' : false,
      },
    },
  })

  // 订阅 store 变更:维护模式切换/注销完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 静默轮询序号:请求在途时若发生翻页/手动刷新,丢弃本轮旧响应,避免旧页数据覆盖新页 */
  let silentSeq = 0
  /** 在途静默请求:卸载时中止,不留一条没人等结果的请求 */
  let silentAbort: AbortController | null = null
  /** 已卸载标记:即便底层不认 abort,也不再把响应写回已销毁的页面 */
  let unmounted = false
  /** 轮询计数:决定哪一轮顺带全量补拉 GPU */
  let pollCount = 0
  /** 上一轮发现当前页有缓存未覆盖的节点:下一轮立刻补拉 GPU,不让这些行空等一个 GPU 补拉周期 */
  let needGpuResync = false

  function abortSilentPoll(): void {
    silentAbort?.abort()
    silentAbort = null
  }
  onBeforeUnmount(() => {
    unmounted = true
    abortSilentPoll()
  })

  /**
   * 静默刷新:按最近一次查询参数重放请求并直接写回表格数据,不翻 loading 遮罩
   * (走 useTable.refresh 会让 n-data-table 每轮闪一次 spin);手动刷新/翻页在途时跳过本轮;
   * 标签页隐藏自动暂停、回到前台立即补一轮(usePolling)。首屏仍由 useTable 的 immediate 负责。
   *
   * 三条省成本的规则:
   * 1) GPU 每 GPU_RESYNC_EVERY_POLLS 轮才全量补一次,其余轮次复用 store 缓存(只拉节点);
   * 2) 当前页出现缓存未覆盖的节点(新注册/翻到新页)时下一轮立刻补拉,不让 GPU 列空着;
   * 3) 结果指纹与上一轮一致就跳过 setData,省掉一次整表重渲。
   * 失败与中止一律静默(轮询失败不弹统一错误提示,由下一次手动刷新兜底),下个周期自动重试。
   */
  async function silentRefresh() {
    const query = lastQuery
    if (!query || loading.value) return
    const seq = ++silentSeq
    abortSilentPoll()
    const controller = new AbortController()
    silentAbort = controller
    let result: Awaited<ReturnType<typeof request>>
    try {
      pollCount += 1
      result = await request(query, { signal: controller.signal }, {
        silentError: true,
        includeGpus: needGpuResync || pollCount % GPU_RESYNC_EVERY_POLLS === 0,
      })
    }
    catch {
      // 中止(卸载/新一轮)或接口失败:本轮作废,usePolling 会在下个周期重试
      return
    }
    finally {
      if (silentAbort === controller) silentAbort = null
    }
    // 请求期间发生卸载/翻页/手动刷新:旧响应不得覆盖新页
    if (unmounted || seq !== silentSeq || loading.value || query !== lastQuery) return
    needGpuResync = result.missingGpus
    const signature = dataSignature(result.data, result.total)
    if (signature === lastSignature) return
    lastSignature = signature
    setData(result.data, result.total)
  }

  /**
   * usePolling 不自启:此处拉起轮询链。mounted 时首屏请求在途(loading=true),silentRefresh 跳过本轮,
   * 下个周期(10s)才真正开始静默刷新,不与 useTable 的 immediate 首刷重复打接口。
   */
  const { refresh: startPolling } = usePolling(silentRefresh, LIST_POLL_INTERVAL_MS)
  onMounted(() => void startPolling())

  /** 视图模式:表格 / 卡片,持久化到 localStorage(按平台前缀) */
  const VIEW_MODE_KEY = 'llmpod:admin:node-list:view-mode'
  const viewMode = ref<'table' | 'card'>(
    (localStorage.getItem(VIEW_MODE_KEY) as 'table' | 'card') || 'table',
  )
  watch(viewMode, (value) => {
    localStorage.setItem(VIEW_MODE_KEY, value)
  })

  /** 注册节点:打开注册弹窗 */
  function openRegister() {
    useDialog({
      title: '注册节点',
      component: NodeRegisterModal,
      area: 56,
      btn: false,
    })
  }

  // 全局搜索「注册节点」:?action=create 进入时自动打开注册弹窗
  useEntryAction({ create: openRegister })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    hasActiveQuery,
    reset,
    alertIcon,
    openRegister,
    activeNode,
    gpuDrawerShow,
    detailDrawerShow,
    viewMode,
    nodes,
    loading,
    openGpuDrawer,
    openDetailDrawer,
    onUpgrade,
  }
}
