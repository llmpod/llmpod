import { message, useTable } from '@repo/hooks'
import type { ComponentPublicInstance } from 'vue'
import type { DataTableColumns, SelectOption } from 'naive-ui'
import { listApiKeys } from '@/api/api-key'
import { listPublishes } from '@/api/publish'
import { listTenants } from '@/api/tenant'
import { usePolling } from '@/views/dashboard-shared/usePolling'
import { chartAxisColors, cssRgb, emptyChartOption, useChart } from '@/views/dashboard-shared/charts'
import {
  colorForKey,
  CUSTOM_RANGE_MAX_MS,
  formatGrouped,
  formatLatency,
  EMPTY_TEXT,
  isFutureDate,
  WINDOW_OPTIONS,
  windowToRealtimeQuery,
} from '@/views/dashboard-shared/format'
import { useStore, type DistView, type ModelRow } from './useStore'

/** 轮询间隔:≤10s 引擎稳快档(§3.0) */
const REFRESH_INTERVAL_MS = 10_000

/** 模型统计宽表列(行数据已在 store 格式化为文案;末行合计加粗走 CSS) */
const modelColumns: DataTableColumns<ModelRow> = [
  { title: '模型', key: 'model', minWidth: 180 },
  { title: '调用量', key: 'calls', width: 110 },
  { title: '占比', key: 'share', width: 90 },
  { title: '平均延迟', key: 'latency', width: 100 },
  { title: '错误率', key: 'error', width: 90 },
  { title: '截断率', key: 'truncation', width: 90 },
  { title: 'input', key: 'input', width: 130 },
  { title: 'output', key: 'output', width: 130 },
  { title: '其中缓存', key: 'cached', width: 130 },
  { title: '合计', key: 'total', width: 100 },
]

/** 四张分布面板(标题 + 对应 store 键) */
const DIST_PANELS = [
  { key: 'latency', title: '耗时分布' },
  { key: 'ttft', title: '首字延迟分布' },
  { key: 'promptLen', title: '输入长度分布' },
  { key: 'generationLen', title: '输出长度分布' },
] as const

export function useController() {
  const {
    activeWindow,
    customRange: storeCustomRange,
    filterModel,
    filterTenantId,
    filterApiKey,
    statCards,
    stacked,
    distributions,
    modelRows,
    sourceText,
    compareText,
    fetchAll,
  } = useStore()

  /** 首屏/切窗加载态:期间整屏骨架;轮询静默不闪骨架 */
  const loading = ref(true)

  // === 模型统计表:数据随整屏拉取后 setData 注入,表格自身不发请求 ===
  // rowKey 防碰撞:合计行 model 文案与其他行理论上可重名,固定 __total__ 兜底
  const { LmTable: ModelTable, loading: tableLoading, setData: setTableData } = useTable<ModelRow>({
    request: async () => ({ data: [], total: 0 }),
    columns: modelColumns,
    rowKey: row => (row.isTotal ? '__total__' : row.model),
    immediate: false,
    config: {
      pagination: false,
      tableProps: {
        flexHeight: false,
        class: 'tabular-nums',
        scrollX: 1120,
        rowClassName: (row: ModelRow) => (row.isTotal ? 'stat-row--total' : ''),
      },
    },
  })
  watch(loading, value => (tableLoading.value = value), { immediate: true })
  watch(modelRows, rows => setTableData(rows))

  // === 筛选选项(挂载后一次性加载,单项失败静默降级为空选项) ===
  // 模型下拉固定首项"全部"(value '' 与未选/clearable 清空对齐,空串时 n-select 不显示 placeholder)
  const modelOptions = ref<SelectOption[]>([{ label: '全部', value: '' }])
  const tenantOptions = ref<SelectOption[]>([])
  const apiKeyOptions = ref<SelectOption[]>([])

  onMounted(() => {
    void listPublishes()
      .then(
        rows =>
          (modelOptions.value = [
            { label: '全部', value: '' },
            ...rows.map(row => ({ label: row.route.name, value: row.route.name })),
          ]),
      )
      .catch(() => {})
    // page_size 100 是后端上限,租户超 100 时下拉截断(取舍:筛选下拉不翻页加载,避免大租户平台首屏多请求)
    void listTenants({ page_size: 100 })
      .then(
        page =>
          (tenantOptions.value = page.items
            .filter(item => item.id !== 0)
            .map(item => ({ label: item.name, value: item.id }))),
      )
      .catch(() => {})
    void listApiKeys()
      .then(rows => (apiKeyOptions.value = rows.map(row => ({ label: row.name, value: row.id }))))
      .catch(() => {})
  })

  // === 堆叠调用趋势(全宽):总量 + 构成同一张图,y 轴为请求数(§6 屏2 #3) ===
  const { el: trendEl, render: renderTrend } = useChart(() => {
    const view = stacked.value
    if (!view) return emptyChartOption()
    const { axisColor, splitColor } = chartAxisColors()
    return {
      tooltip: {
        trigger: 'axis',
        confine: true,
        axisPointer: { type: 'shadow' },
        formatter: (params: Array<{ dataIndex: number; seriesName: string; value: number }>) => {
          const index = params[0]?.dataIndex ?? 0
          const latency = view.latencyMs[index] ?? null
          const breakdown = view.series
            .map(series => ({ key: series.key, value: series.values[index] ?? 0 }))
            .filter(item => item.value > 0)
            .sort((a, b) => b.value - a.value)
            .slice(0, 6)
          const total = view.requests[index] ?? 0
          const lines = [
            view.labels[index],
            `合计 <b>${total.toLocaleString()}</b>`,
            `平均延迟 ${latency === null ? EMPTY_TEXT : formatLatency(latency)}`,
          ]
          if (breakdown.length) lines.push(`<div style="margin-top:4px;border-top:1px solid ${splitColor};padding-top:4px">`)
          breakdown.forEach((item) => {
            const shareText = total === 0 ? '0%' : `${((item.value / total) * 100).toFixed(0)}%`
            lines.push(
              `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;background:${colorForKey(item.key)}"></span>${item.key} <b>${item.value.toLocaleString()}</b>(${shareText})`,
            )
          })
          if (breakdown.length) lines.push('</div>')
          return lines.join('<br/>')
        },
      },
      legend: {
        data: view.series.map(series => series.key),
        textStyle: { color: axisColor },
        top: 0,
        right: 0,
        type: 'scroll',
      },
      grid: { left: 8, right: 8, top: 36, bottom: 0, containLabel: true },
      xAxis: {
        type: 'category',
        data: view.labels,
        axisTick: { show: false },
        axisLabel: { color: axisColor, hideOverlap: true },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: axisColor, formatter: (value: number) => formatGrouped(value, view.axisMax) },
        splitLine: { lineStyle: { color: splitColor, type: 'dashed' } },
      },
      series: view.series.map(series => ({
        name: series.key,
        type: 'bar',
        stack: 'total',
        data: series.values,
        barMaxWidth: 28,
        itemStyle: { color: colorForKey(series.key) },
      })),
    }
  })

  // === 四张分布(桶已在 store 差分;轴色随主题重绘时现取) ===
  const distCharts = {
    latency: useChart(() => buildDistOption(distributions.value.latency)),
    ttft: useChart(() => buildDistOption(distributions.value.ttft)),
    promptLen: useChart(() => buildDistOption(distributions.value.promptLen)),
    generationLen: useChart(() => buildDistOption(distributions.value.generationLen)),
  }

  /**
   * 分布图容器函数式 ref 绑定:v-for 内 :ref 直绑 ref 对象会被 Vue 赋成数组,
   * echarts.init 收到数组直接抛 getContext TypeError(容器常驻后暴露的存量问题)
   */
  function setDistEl(key: keyof typeof distCharts, el: Element | ComponentPublicInstance | null) {
    distCharts[key].el.value = el instanceof HTMLElement ? el : undefined
  }

  /** 分布直方图 option(counts/shareText 已在 store 计算好;无样本 → 空图表骨架) */
  function buildDistOption(dist: DistView | null) {
    if (!dist) return emptyChartOption()
    const { axisColor, splitColor } = chartAxisColors()
    const counts = dist.bars.map(bar => bar.count)
    return {
      tooltip: {
        trigger: 'axis',
        confine: true,
        axisPointer: { type: 'shadow' },
        formatter: (params: Array<{ dataIndex: number }>) => {
          const index = params[0]?.dataIndex ?? 0
          const shareText = dist.bars[index]?.shareText
          return `${dist.labels[index]}<br/>计数 <b>${counts[index]?.toLocaleString() ?? '0'}</b>${shareText ? ` · ${shareText}` : ''}`
        },
      },
      grid: { left: 8, right: 8, top: 20, bottom: 0, containLabel: true },
      xAxis: {
        type: 'category',
        data: dist.labels,
        axisTick: { show: false },
        // x 轴抽稀:窄卡里桶标签全显会过密(13 桶/251px),自动间隔 + 隐藏放不下的标签
        axisLabel: { color: axisColor, hideOverlap: true },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: axisColor },
        splitLine: { lineStyle: { color: splitColor, type: 'dashed' } },
      },
      series: [
        {
          type: 'bar',
          data: counts,
          barMaxWidth: 24,
          itemStyle: { color: cssRgb('--n-primary-color', '#5b5bd6'), borderRadius: [3, 3, 0, 0] },
        },
      ],
    }
  }

  /** 重绘全部图表(骨架屏期间容器 display:none,须在数据就绪后 nextTick 调用) */
  function renderAllCharts() {
    void renderTrend()
    Object.values(distCharts).forEach(chart => void chart.render())
  }

  // === 请求竞态:切窗/切筛选中止在途旧请求(旧参数响应不再落盘),序列守卫保证只有最新一轮收尾 loading ===
  let abortController: AbortController | undefined
  let restartSeq = 0

  async function fetchAllAndRender() {
    abortController?.abort() // 中止在途旧请求(旧参数响应不再落盘)
    abortController = new AbortController()
    try {
      await fetchAll(abortController.signal)
    } catch {
      // 被更新的请求取代或单次失败:静默返回,由轮询下个周期重试(usePolling 吞错)
      return
    }
    if (!loading.value) renderAllCharts()
  }

  const { refresh: poll } = usePolling(fetchAllAndRender, REFRESH_INTERVAL_MS)

  /** 首屏:首轮拉完出骨架(与切窗同一守卫路径,首轮在途被打断时不提前收尾) */
  onMounted(() => {
    void restart()
  })

  /** 窗口切换:回骨架立即带新窗口重拉(§6 屏2 #2);custom 只能由日期选择器进入,tab 不会传它 */
  function setWindow(value: typeof activeWindow.value) {
    if (activeWindow.value === value) return
    activeWindow.value = value
    void restart()
  }

  /**
   * 日期选择器展示区间:快捷窗口 = 该窗口当前实际区间(to 随轮询自然前移),
   * custom = 已提交的自定义区间。驱动页头常驻 date-picker 的显示。
   */
  const customRange = computed<[number, number] | null>(() => {
    const now = Date.now()
    if (activeWindow.value === 'custom') return storeCustomRange.value
    const { from, to } = windowToRealtimeQuery(activeWindow.value, new Date(now))
    return [Date.parse(from), Date.parse(to)]
  })

  /**
   * 日期选择器确认(datetimerange,输入框只显日期、面板可调时间):区间原样下推切 custom 重拉;
   * 倒置/超 90 天不生效并提示。null(清空)回退今日窗口。
   */
  function setCustomRange(range: [number, number] | null) {
    if (range === null) {
      storeCustomRange.value = null
      if (activeWindow.value === 'custom') setWindow('today')
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
    if (activeWindow.value !== 'custom') activeWindow.value = 'custom'
    void restart()
  }

  /** 整屏筛选切换:回骨架重拉(§6 屏2 #4);三项均未变化时短路不拉 */
  function setFilter(patch: { model?: string; tenantId?: number | null; apiKeyId?: number | null }) {
    const nextModel = patch.model !== undefined ? patch.model : filterModel.value
    const nextTenant = patch.tenantId !== undefined ? patch.tenantId : filterTenantId.value
    const nextApiKey = patch.apiKeyId !== undefined ? patch.apiKeyId : filterApiKey.value
    if (nextModel === filterModel.value && nextTenant === filterTenantId.value && nextApiKey === filterApiKey.value) return
    filterModel.value = nextModel
    filterTenantId.value = nextTenant
    filterApiKey.value = nextApiKey
    void restart()
  }

  async function restart() {
    const seq = ++restartSeq
    loading.value = true
    tableLoading.value = true
    await poll()
    // 已有更新的 restart 接管(连点切窗):本次不收尾,避免骨架提前消失
    if (seq !== restartSeq) return
    loading.value = false
    nextTick(renderAllCharts)
  }

  return {
    loading,
    windowOptions: WINDOW_OPTIONS,
    activeWindow,
    customRange,
    setWindow,
    setCustomRange,
    isDateDisabled: isFutureDate,
    setFilter,
    filterModel,
    filterTenantId,
    filterApiKey,
    modelOptions,
    tenantOptions,
    apiKeyOptions,
    statCards,
    compareText,
    sourceText,
    trendEl,
    distCharts,
    setDistEl,
    DIST_PANELS,
    distributions,
    ModelTable,
  }
}
