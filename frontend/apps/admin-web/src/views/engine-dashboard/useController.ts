import { message, useTable } from '@repo/hooks'
import { usePolling } from '@/views/dashboard-shared/usePolling'
import { chartAxisColors, emptyChartOption, useChart } from '@/views/dashboard-shared/charts'
import {
  colorForKey,
  CUSTOM_RANGE_MAX_MS,
  EMPTY_TEXT,
  formatGrouped,
  isFutureDate,
  WINDOW_OPTIONS,
  windowToRealtimeQuery,
} from '@/views/dashboard-shared/format'
import {
  anomalyBarColor,
  KV_SATURATION_RATE,
  successRateTextClass,
  useStore,
  type AnomalyItemView,
  type UpstreamHealthItem,
} from './useStore'
import { NProgress } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'

/** /realtime 轮询间隔:≤10s 引擎稳快档(§3.0,屏3 网格吃同一端点) */
const REALTIME_INTERVAL_MS = 10_000
/** KV 热力图:打开即拉 + 60s(§0) */
const HISTORY_INTERVAL_MS = 60_000
/** 异常 + 上游健康:30s(§0) */
const ANOMALY_INTERVAL_MS = 30_000

/** 热力图色阶(0-1 连续渐变,贴近设计稿 ░<40% ▒40-70 ▓70-85 █>85) */
const HEAT_COLORS = ['#6fbf8b', '#f2d24c', '#ef8f4e', '#e05252']

/** 上游健康表格列:成功率按数值分档着色(≥99% 绿 / 95–99% 中性 / 90–95% 橙 / <90% 红),窄屏卡片复用同一 render */
const upstreamColumns: DataTableColumns<UpstreamHealthItem> = [
  { title: '上游', key: 'name', minWidth: 180, ellipsis: { tooltip: true } },
  {
    title: '成功率',
    key: 'successText',
    width: 120,
    align: 'right',
    titleAlign: 'right',
    className: 'tabular-nums',
    render: row => {
      const colorClass = successRateTextClass(row.successRate)
      return colorClass ? h('span', { class: colorClass }, row.successText) : row.successText
    },
  },
  { title: '平均延迟', key: 'latencyText', width: 120, align: 'right', titleAlign: 'right', className: 'tabular-nums' },
  { title: 'QPM', key: 'qpmText', width: 100, align: 'right', titleAlign: 'right', className: 'tabular-nums' },
]

/** 异常构成表格列:占比列内联进度条(条色按归属:准入拒绝橙 / 上游错误红),合计与占总请求留在卡头 */
const anomalyColumns: DataTableColumns<AnomalyItemView> = [
  { title: '异常类型', key: 'label', ellipsis: { tooltip: true } },
  { title: '次数', key: 'count', className: 'tabular-nums' },
  {
    title: '占比',
    key: 'shareText',
    align: 'right',
    titleAlign: 'right',
    render: row =>
      h('div', { class: 'flex items-center gap-[.5rem]' }, [
        h('div', { class: 'flex-1 min-w-0' }, [
          h(NProgress, {
            type: 'line',
            percentage: row.sharePct,
            showIndicator: false,
            height: 8,
            borderRadius: 4,
            color: anomalyBarColor(row.side),
          }),
        ]),
        h('span', { class: 'tabular-nums text-right shrink-0 whitespace-nowrap' }, row.shareText),
      ]),
  },
]

export function useController() {  const {
    realtimeWindow,
    customRange: storeCustomRange,
    kpiCards,
    engineRows,
    throughput,
    heatmap,
    anomalyView,
    upstreamHealth,
    banner,
    tsText,
    fetchRealtime,
    fetchHistory,
    fetchAnomaly,
  } = useStore()

  /** 三路独立首屏加载(骨架屏按面板分组);轮询静默不闪骨架 */
  const realtimeLoading = ref(true)
  const historyLoading = ref(true)
  const anomalyLoading = ref(true)

  /** 吞吐趋势索引化显示(首槽=100);baseline 不可索引时隐藏开关(§3.2) */
  const indexed = ref(false)
  const canIndex = computed(() => throughput.value?.canIndex ?? false)

  /** 窗口中文文案(上游健康/异常卡片副标题;custom 显示区间文案) */
  const realtimeWindowLabel = computed(() =>
    realtimeWindow.value === 'custom' ? '自定义区间' : WINDOW_OPTIONS.find(option => option.value === realtimeWindow.value)?.label ?? realtimeWindow.value,
  )

  /** 上游健康表格行:本地/外部通道在前、外部供应商在后,平铺一张表 */
  const upstreamRows = computed(() => {
    const health = upstreamHealth.value
    if (!health) return []
    return [health.local, health.external, ...health.providers].filter((item): item is UpstreamHealthItem => !!item)
  })
  const hasUpstreamData = computed(() => upstreamRows.value.length > 0)
  const hasUpstreamProviders = computed(() => (upstreamHealth.value?.providers.length ?? 0) > 0)

  // === 上游健康表:数据随 anomaly 轮询落地后 setData 注入,表格自身不发请求 ===
  // 窄屏(≤767px)LmTable 自动切卡片:名称做主标题、成功率提卡首强调位、attention 行走 warning 边轨
  const { LmTable: UpstreamTable, setData: setUpstreamData } = useTable<UpstreamHealthItem>({
    request: async () => ({ data: [], total: 0 }),
    columns: upstreamColumns,
    rowKey: row => row.name,
    immediate: false,
    config: {
      pagination: false,
      tableProps: { flexHeight: false, maxHeight: 360, scrollX: 520, size: 'small', bordered: false },
      card: {
        titleKey: 'name',
        fieldKeys: ['successText','latencyText', 'qpmText'],
        alertWhen: row => (row.attention ? 'warning' : false),
      },
    },
  })
  watch(upstreamRows, rows => setUpstreamData(rows, rows.length))

  /** 异常构成表格行:后端已按占比降序返回,原样铺表(合计/占总请求仍在卡头) */
  const anomalyRows = computed(() => anomalyView.value?.items ?? [])
  const { LmTable: AnomalyTable, setData: setAnomalyData } = useTable<AnomalyItemView>({
    request: async () => ({ data: [], total: 0 }),
    columns: anomalyColumns,
    rowKey: row => `${row.sideText}-${row.label}`,
    immediate: false,
    config: {
      pagination: false,
      tableProps: { flexHeight: false, maxHeight: 360, scrollX: 400, size: 'small', bordered: false },
      card: {
        titleKey: 'label',
        fieldKeys: ['count', 'shareText'],
      },
    },
  })
  watch(anomalyRows, rows => setAnomalyData(rows, rows.length))

  /** 吞吐趋势:QPM/TPM 双线;索引态显示倍数(基准=首槽;轴色随主题重绘时现取) */
  const { el: throughputEl, render: renderThroughput } = useChart(() => {
    const view = throughput.value
    if (!view) return emptyChartOption()
    const { axisColor, splitColor } = chartAxisColors()
    const scale = (values: number[], baseline: number | null) =>
      baseline && baseline > 0 && indexed.value
        ? values.map(value => Number(((value / baseline) * 100).toFixed(1)))
        : values
    return {
      tooltip: {
        trigger: 'axis',
        confine: true,
        axisPointer: { type: 'cross', label: { backgroundColor: 'rgba(128,128,128,.6)' } },
      },
      legend: { data: ['QPM', 'TPM'], textStyle: { color: axisColor }, top: 0, right: 0 },
      grid: { left: 8, right: 8, top: 36, bottom: 0, containLabel: true },
      xAxis: {
        type: 'category',
        data: view.labels,
        boundaryGap: false,
        axisTick: { show: false },
        axisLabel: { color: axisColor },
      },
      yAxis: {
        type: 'value',
        axisLabel: {
          color: axisColor,
          formatter: (value: number) => (indexed.value ? String(value) : formatGrouped(value, Math.max(...view.tpm, 1))),
        },
        splitLine: { lineStyle: { color: splitColor, type: 'dashed' } },
      },
      series: [
        {
          name: 'QPM',
          type: 'line',
          data: scale(view.qpm, view.baselineQpm),
          smooth: true,
          symbol: 'none',
          itemStyle: { color: colorForKey('__qpm__') },
        },
        {
          name: 'TPM',
          type: 'line',
          data: scale(view.tpm, view.baselineTpm),
          smooth: true,
          symbol: 'none',
          itemStyle: { color: colorForKey('__tpm__') },
        },
      ],
    }
  })

  /** KV 热力图:行=实例,列=5min 桶;缺桶空档(不渲染数据点);抢占>0 描边标记(§4.1) */
  const { el: heatmapEl, render: renderHeatmap } = useChart(() => {
    const view = heatmap.value
    if (!view) return emptyChartOption()
    const { axisColor } = chartAxisColors()
    const data: Array<{ value: [number, number, number]; itemStyle?: { borderColor: string; borderWidth: number } }> = []
    view.rows.forEach((row, rowIndex) => {
      row.cells.forEach((cell, colIndex) => {
        if (!cell) return // 缺桶 = 空档,不补 0
        data.push({
          value: [colIndex, rowIndex, cell.kvAvg],
          ...(cell.preemptions > 0
            ? { itemStyle: { borderColor: 'rgb(240,160,32)', borderWidth: 1.5 } }
            : {}),
        })
      })
    })
    return {
      tooltip: {
        trigger: 'item',
        confine: true,
        formatter: (params: { value: [number, number, number] }) => {
          const [colIndex, rowIndex] = params.value
          const cell = view.rows[rowIndex]?.cells[colIndex]
          if (!cell) return ''
          const max = cell.kvMax === 0 ? EMPTY_TEXT : `${Math.round(cell.kvMax * 100)}%`
          return [
            `${view.labels[colIndex]} · ${view.rows[rowIndex]!.label}`,
            `KV 均值 ${Math.round(cell.kvAvg * 100)}% · 峰值 ${max}`,
            `抢占 ${cell.preemptions} · 采样 ${cell.samples} 次`,
          ].join('<br/>')
        },
      },
      grid: { left: 8, right: 8, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: 'category',
        data: view.labels,
        axisTick: { show: false },
        // 时间桶可达数百个(24h→288),auto 抽稀 + 隐藏仍重叠的标签
        axisLabel: { color: axisColor, hideOverlap: true, rotate: view.labels.length > 8 ? 45 : 0 },
        splitArea: { show: true, areaStyle: { color: ['rgba(128,128,128,.04)', 'rgba(128,128,128,.08)'] } },
      },
      yAxis: { type: 'category', data: view.rows.map(row => row.label), axisLabel: { color: axisColor } },
      visualMap: {
        min: 0,
        max: 1,
        show: false,
        inRange: { color: HEAT_COLORS },
      },
      series: [
        {
          type: 'heatmap',
          data,
          itemStyle: { borderColor: 'rgba(128,128,128,.15)', borderWidth: 1 },
          emphasis: { itemStyle: { shadowBlur: 4, shadowColor: 'rgba(0,0,0,.3)' } },
        },
      ],
    }
  })

  // === 请求竞态(三路各自独立):切窗/重启中止在途旧请求,序列守卫保证只有最新一轮收尾 loading ===
  let realtimeAbort: AbortController | undefined
  let historyAbort: AbortController | undefined
  let anomalyAbort: AbortController | undefined
  let realtimeSeq = 0
  let historySeq = 0
  let anomalySeq = 0

  async function fetchRealtimeAll() {
    realtimeAbort?.abort() // 中止在途旧请求(旧参数响应不再落盘)
    realtimeAbort = new AbortController()
    try {
      await fetchRealtime(realtimeAbort.signal)
    } catch {
      // 被更新的请求取代或单次失败:静默返回,由轮询下个周期重试(usePolling 吞错)
      return
    }
    if (!realtimeLoading.value) void renderThroughput()
  }

  async function fetchHistoryAll() {
    historyAbort?.abort()
    historyAbort = new AbortController()
    try {
      await fetchHistory(historyAbort.signal)
    } catch {
      return
    }
    if (!historyLoading.value) void renderHeatmap()
  }

  async function fetchAnomalyAll() {
    anomalyAbort?.abort()
    anomalyAbort = new AbortController()
    try {
      await fetchAnomaly(anomalyAbort.signal)
    } catch {
      return
    }
  }

  const realtimePoller = usePolling(fetchRealtimeAll, REALTIME_INTERVAL_MS)
  const historyPoller = usePolling(fetchHistoryAll, HISTORY_INTERVAL_MS)
  const anomalyPoller = usePolling(fetchAnomalyAll, ANOMALY_INTERVAL_MS)

  /** 首屏:三路并行 restart(与切窗同一守卫路径,首轮在途被打断时不提前收尾) */
  onMounted(() => {
    void restartRealtime()
    void restartHistory()
    void restartAnomaly()
  })

  /** 窗口切换(全屏唯一窗口):三路联动重拉,加载标志各自收尾;custom 只能由日期选择器进入,tab 不会传它 */
  function setRealtimeWindow(value: typeof realtimeWindow.value) {
    if (realtimeWindow.value === value) return
    realtimeWindow.value = value
    void restartRealtime()
    void restartHistory()
    void restartAnomaly()
  }

  /**
   * 日期选择器展示区间:快捷窗口 = 该窗口当前实际区间(to 随轮询自然前移),
   * custom = 已提交的自定义区间。驱动页头常驻 date-picker 的显示。
   */
  const customRange = computed<[number, number] | null>(() => {
    if (realtimeWindow.value === 'custom') return storeCustomRange.value
    const { from, to } = windowToRealtimeQuery(realtimeWindow.value)
    return [Date.parse(from), Date.parse(to)]
  })

  /**
   * 日期选择器确认(datetimerange,输入框只显日期、面板可调时间):区间原样下推切 custom 三路重拉;
   * 倒置/超 90 天不生效并提示。null(清空)回退 1h 窗口。
   */
  function setCustomRange(range: [number, number] | null) {
    if (range === null) {
      storeCustomRange.value = null
      if (realtimeWindow.value === 'custom') setRealtimeWindow('1h')
      return
    }
    const [from, to] = range
    if (Number.isNaN(from) || Number.isNaN(to) || to <= from) {
      message.error('自定义区间无效:结束时间需晚于开始时间')
      return
    }
    if (to - from > CUSTOM_RANGE_MAX_MS) {
      message.error('自定义区间最长 90 天')
      return
    }
    storeCustomRange.value = [from, to]
    if (realtimeWindow.value !== 'custom') realtimeWindow.value = 'custom'
    void restartRealtime()
    void restartHistory()
    void restartAnomaly()
  }

  async function restartRealtime() {
    const seq = ++realtimeSeq
    realtimeLoading.value = true
    await realtimePoller.refresh()
    // 已有更新的 restart 接管(连点切窗):本次不收尾,避免骨架提前消失
    if (seq !== realtimeSeq) return
    realtimeLoading.value = false
    nextTick(() => void renderThroughput())
  }

  async function restartHistory() {
    const seq = ++historySeq
    historyLoading.value = true
    await historyPoller.refresh()
    if (seq !== historySeq) return
    historyLoading.value = false
    nextTick(() => void renderHeatmap())
  }

  async function restartAnomaly() {
    const seq = ++anomalySeq
    anomalyLoading.value = true
    await anomalyPoller.refresh()
    if (seq !== anomalySeq) return
    anomalyLoading.value = false
  }

  /** 索引化切换:图表容器 v-show 常驻,改值后立即重绘(基准=首槽=100) */
  function toggleIndexed(value: boolean) {
    indexed.value = value
    void renderThroughput()
  }

  return {
    realtimeLoading,
    historyLoading,
    anomalyLoading,
    realtimeWindow,
    realtimeWindowLabel,
    customRange,
    windowOptions: WINDOW_OPTIONS,
    setRealtimeWindow,
    setCustomRange,
    isDateDisabled: isFutureDate,
    indexed,
    canIndex,
    toggleIndexed,
    banner,
    kpiCards,
    engineRows,
    throughputEl,
    heatmapEl,
    anomalyView,
    upstreamHealth,
    UpstreamTable,
    hasUpstreamData,
    hasUpstreamProviders,
    AnomalyTable,
    tsText,
    KV_SATURATION_RATE,
  }
}
