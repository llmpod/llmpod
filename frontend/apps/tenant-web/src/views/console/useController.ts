import { lazyRetryable, useTheme } from '@repo/hooks'
import {
  AddOutline,
  BarChartOutline,
  FlashOutline,
  PeopleOutline,
  SettingsOutline,
  TimeOutline,
} from '@vicons/ionicons5'
import type * as echarts from 'echarts/core'
import { useAuthStore } from '@/store/auth'
import { useStore } from './useStore'

/**
 * echarts(core+charts+components+renderers 约 500KB)按需加载:
 * 趋势图在骨架屏卸载、数据到达后才挂载,不在首屏关键路径上静态打包。
 * 失败即清缓存(lazyRetryable):发版后旧 chunk hash 404 时下次调用重新拉取,
 * 不把 rejected Promise 永久缓存在模块级,导致会话内图表不可恢复。
 */
const ensureEcharts = lazyRetryable(async () => {
  const [core, charts, components, renderers] = await Promise.all([
    import('echarts/core'),
    import('echarts/charts'),
    import('echarts/components'),
    import('echarts/renderers'),
  ])
  core.use([charts.BarChart, components.GridComponent, components.TooltipComponent, renderers.CanvasRenderer])
  return core
})

/** 定时刷新间隔:工作台整屏聚合每 30s 静默拉一轮 */
const REFRESH_INTERVAL_MS = 30_000

/** 主题色以 r,g,b 逗号串注入文档根节点(--n-*-color),拼接为 rgb() 供 echarts 使用 */
function cssRgb(name: string, fallback: string) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value ? `rgb(${value})` : fallback
}
function cssVar(name: string, fallback: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

export function useController() {
  const auth = useAuthStore()
  const router = useRouter()
  const { darkMode } = useTheme()
  const { statCards, trendDays, trendCalls, myModels, recentCalls, wallet, fetchConsole } =
    useStore()

  /** 页面加载态:数据请求期间展示骨架屏 */
  const loading = ref(true)

  /** 按当前时段生成问候语 */
  const greeting = computed(() => {
    const hour = new Date().getHours()
    if (hour < 6) return '凌晨好'
    if (hour < 12) return '早上好'
    if (hour < 14) return '中午好'
    if (hour < 18) return '下午好'
    return '晚上好'
  })

  /** 快捷操作(跳转对应功能页) */
  const quickActions = [
    { label: '创建API Key', icon: AddOutline, name: 'api-key' },
    { label: '在线调试', icon: FlashOutline, name: 'playground' },
    { label: '团队管理', icon: PeopleOutline, name: 'team' },
    { label: '调用统计', icon: BarChartOutline, name: 'statistics' },
    { label: '设置', icon: SettingsOutline, name: 'config' },
  ] as const

  /** 跳转指定路由 */
  function goRoute(name: string) {
    void router.push({ name })
  }

  const trendChartEl = ref<HTMLElement>()
  let trendChart: echarts.ECharts | undefined
  let resizeObserver: ResizeObserver | undefined

  function buildTrendOption(): Record<string, unknown> {
    const primary = cssRgb('--n-primary-color', '#5b5bd6')
    const axisColor = cssVar('--c-secondary', '#8a8f99')
    const splitColor = cssVar('--c-line', '#e5e7eb')
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 8, right: 8, top: 24, bottom: 0, containLabel: true },
      xAxis: {
        type: 'category',
        data: trendDays.value,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: splitColor } },
        axisLabel: { color: axisColor },
      },
      yAxis: {
        type: 'value',
        axisLabel: {
          color: axisColor,
          formatter: (value: number) => (value >= 10000 ? `${value / 10000}万` : String(value)),
        },
        splitLine: { lineStyle: { color: splitColor, type: 'dashed' } },
      },
      series: [
        {
          name: '调用量',
          type: 'bar',
          data: trendCalls.value,
          barWidth: 32,
          label: { show: true, position: 'top', color: axisColor, fontSize: 11 },
          itemStyle: { color: primary, borderRadius: [3, 3, 0, 0] },
        },
      ],
    }
  }

  async function renderTrendChart() {
    if (!trendChartEl.value) return
    // 首次渲染时拉 echarts;骨架屏期间容器未挂载,天然不会提前触发。
    // 加载失败(如发版后旧 chunk 404)静默留空:缓存已清,下次重绘(轮询/主题切换)自动重试,
    // 吞掉 rejection 避免调用侧 void renderTrendChart() 产生 unhandled rejection。
    const echarts = await ensureEcharts().catch(() => null)
    if (!echarts || !trendChartEl.value) return
    if (!trendChart) trendChart = echarts.init(trendChartEl.value)
    trendChart.setOption(buildTrendOption(), { notMerge: true })
  }

  function mountTrendChart() {
    void renderTrendChart()
    resizeObserver = new ResizeObserver(() => trendChart?.resize())
    if (trendChartEl.value) resizeObserver.observe(trendChartEl.value)
  }

  /** 定时刷新:可见时每 30s 静默拉一轮(失败静默重试);标签页隐藏暂停,回到前台立即刷新 */
  let refreshTimer: ReturnType<typeof setTimeout> | undefined
  let disposed = false

  function clearRefreshTimer() {
    if (refreshTimer !== undefined) {
      clearTimeout(refreshTimer)
      refreshTimer = undefined
    }
  }

  function scheduleNextRefresh() {
    if (disposed || document.hidden) return
    // 已有排期不重复排,保证轮询链始终只有一条
    if (refreshTimer !== undefined) return
    refreshTimer = setTimeout(() => void refresh(), REFRESH_INTERVAL_MS)
  }

  async function refresh() {
    if (disposed) return
    clearRefreshTimer()
    try {
      await fetchConsole()
      // 趋势数据更新后重绘(骨架屏阶段容器未挂载,renderTrendChart 内部有守卫)
      void renderTrendChart()
    } catch {
      // 单次刷新失败静默跳过,下个周期自动重试
    }
    scheduleNextRefresh()
  }

  function onVisibilityChange() {
    if (document.hidden) clearRefreshTimer()
    else void refresh()
  }

  onMounted(() => {
    void fetchConsole()
      .catch(() => {})
      .then(() => {
        // 失败也卸下骨架屏(空数据兜底由视图层承担),首屏 Promise 只负责一次
        loading.value = false
        // 骨架屏卸载后图表容器才进入 DOM,需 nextTick 后再挂载
        nextTick(mountTrendChart)
      })
      .finally(scheduleNextRefresh)
    document.addEventListener('visibilitychange', onVisibilityChange)
  })
  onUnmounted(() => {
    disposed = true
    clearRefreshTimer()
    resizeObserver?.disconnect()
    trendChart?.dispose()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  /** 明暗主题切换后重取 CSS 变量刷新图表配色;
   * 需 nextTick 延迟:vueuse useColorMode 在 post 阶段才更新 html.dark 类名,
   * 同步重绘会读到上一主题的 --c-* 变量(暗→亮时 splitLine 仍为半透明白,刻度线不可见) */
  watch(darkMode, () => {
    if (!loading.value) nextTick(renderTrendChart)
  })

  return {
    loading,
    greeting,
    username: computed(() => auth.userInfo?.username),
    quickActions,
    goRoute,
    statCards,
    trendChartEl,
    myModels,
    recentCalls,
    wallet,
    TimeOutline,
  }
}
