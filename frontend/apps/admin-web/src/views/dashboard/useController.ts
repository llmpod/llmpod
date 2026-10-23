import { useStore } from './useStore'
import { useAuthStore } from '@/store/auth'
import { usePolling } from '@/views/dashboard-shared/usePolling'
import { chartAxisColors, cssRgb, cssVar, useChart } from '@/views/dashboard-shared/charts'
import { formatGrouped, maxOf } from '@/views/dashboard-shared/format'

/** 定时刷新间隔:平台总览整屏每 30s 静默拉一轮(§0) */
const REFRESH_INTERVAL_MS = 30_000

const WEEK_TEXT = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

function pad(value: number) {
  return String(value).padStart(2, '0')
}

export function useController() {
  const router = useRouter()
  const auth = useAuthStore()
  const {
    kpiCards,
    trendView,
    trendTotalsText,
    upstreamCards,
    banner,
    serviceInfo,
    nodesInfo,
    gpuInfo,
    gpuPools,
    deploySummary,
    deployList,
    fetchOverview,
  } = useStore()

  /** 首屏加载态:数据请求期间展示骨架屏;后续轮询静默更新 */
  const loading = ref(true)

  /** 顶部实时时钟 */
  const now = ref(new Date())
  const clockTimer = window.setInterval(() => {
    now.value = new Date()
  }, 1000)
  onUnmounted(() => window.clearInterval(clockTimer))

  /** 调用趋势双线图:今日=主色实线,昨日同期=次色虚线;y 轴单位按组内最大值(§1.5) */
  const { el: trendEl, render: renderTrend } = useChart(() => {
    const { axisColor, splitColor } = chartAxisColors()
    const todayColor = cssRgb('--n-primary-color', '#5b5bd6')
    const yesterdayColor = cssVar('--c-secondary', '#8a8f99')
    // §1.5 组=整张图(两序列共用 y 轴),取今日/昨日整体最大值定单位
    const axisMax = Math.max(maxOf(trendView.value.today), maxOf(trendView.value.yesterday))
    return {
      tooltip: { trigger: 'axis', confine: true },
      legend: {
        data: ['今日', '昨日同期'],
        textStyle: { color: axisColor },
        top: 0,
        right: 0,
        itemWidth: 14,
        itemHeight: 8,
      },
      grid: { left: 8, right: 8, top: 36, bottom: 0, containLabel: true },
      xAxis: {
        type: 'category',
        data: trendView.value.labels,
        boundaryGap: false,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: splitColor } },
        axisLabel: { color: axisColor, hideOverlap: true },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: axisColor, formatter: (value: number) => formatGrouped(value, axisMax) },
        splitLine: { lineStyle: { color: splitColor, type: 'dashed' } },
      },
      series: [
        {
          name: '今日',
          type: 'line',
          data: trendView.value.today,
          smooth: true,
          symbol: 'none',
          lineStyle: { color: todayColor, width: 2 },
          itemStyle: { color: todayColor },
        },
        {
          name: '昨日同期',
          type: 'line',
          data: trendView.value.yesterday,
          smooth: true,
          symbol: 'none',
          lineStyle: { color: yesterdayColor, width: 2, type: 'dashed' },
          itemStyle: { color: yesterdayColor },
        },
      ],
    }
  })

  /** 轮询 fetcher:拉取后重绘趋势图(首轮渲染由 onMounted 的 nextTick 触发) */
  async function fetchAll() {
    await fetchOverview()
    if (!loading.value) void renderTrend()
  }

  const { refresh: poll } = usePolling(fetchAll, REFRESH_INTERVAL_MS)

  onMounted(() => {
    void poll().then(() => {
      loading.value = false
      nextTick(() => void renderTrend())
    })
  })

  const timeText = computed(
    () => `${pad(now.value.getHours())}:${pad(now.value.getMinutes())}:${pad(now.value.getSeconds())}`,
  )
  const dateText = computed(
    () =>
      `${now.value.getFullYear()}/${pad(now.value.getMonth() + 1)}/${pad(now.value.getDate())} ${WEEK_TEXT[now.value.getDay()]}`,
  )

  /** 上游需关注供应商数(无上游数据时 0) */
  const upstreamAttentionCount = computed(
    () => upstreamCards.value?.providers.filter(item => item.attention).length ?? 0,
  )

  /** 横幅标题:正常 / N 项需关注 */
  const bannerTitleText = computed(() => (banner.value.ok ? '正常' : `${banner.value.items.length} 项需关注`))

  /** 横幅摘要:需关注项以 · 连接;正常时为空 */
  const bannerDetailText = computed(() => (banner.value.ok ? '' : banner.value.items.join(' · ')))

  /** 跳转开关:目标页面无菜单权限时隐藏跳转入口(口径同路由守卫 hasMenu) */
  const canDeploy = computed(() => auth.hasMenu('deploy'))
  const canNodePool = computed(() => auth.hasMenu('node-pool'))

  /** 跳转部署列表并按状态自动筛选 */
  function goDeployStatus(status: string) {
    void router.push({ name: 'deploy', query: { action: 'status', status } })
  }

  /** 跳转指定部署详情视图 */
  function goDeployDetail(id: string) {
    void router.push({ name: 'deploy', query: { action: 'detail', id } })
  }

  /** 跳转集群拓扑详情(?topology=<clusterId> 由集群列表页解析) */
  function goClusterDetail(id: number) {
    void router.push({ name: 'node-pool', query: { topology: String(id) } })
  }

  return {
    loading,
    timeText,
    dateText,
    banner,
    bannerTitleText,
    bannerDetailText,
    kpiCards,
    trendEl,
    trendTotalsText,
    upstreamCards,
    upstreamAttentionCount,
    serviceInfo,
    nodesInfo,
    gpuInfo,
    gpuPools,
    deploySummary,
    deployList,
    canDeploy,
    canNodePool,
    goDeployStatus,
    goDeployDetail,
    goClusterDetail,
  }
}
