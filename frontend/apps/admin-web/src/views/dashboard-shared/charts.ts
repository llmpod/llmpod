import { lazyRetryable, useTheme } from '@repo/hooks'
import type * as echarts from 'echarts/core'

/**
 * echarts(core+charts+components+renderers 约 580KB)按需加载:
 * 图表容器就绪、数据到达后才触发,不进 dashboard 落地页首屏关键路径。
 * 失败即清缓存(lazyRetryable):发版后旧 chunk hash 404 时下次调用重新拉取,
 * 不会把 rejected Promise 永久缓存在模块级,导致会话内图表不可恢复。
 */
export const ensureEcharts = lazyRetryable(async () => {
  const [core, charts, components, renderers] = await Promise.all([
    import('echarts/core'),
    import('echarts/charts'),
    import('echarts/components'),
    import('echarts/renderers'),
  ])
  // 三屏按需注册(新增图表类型在此追加,保持 tree-shaking)
  core.use([
    charts.LineChart,
    charts.BarChart,
    charts.HeatmapChart,
    components.GridComponent,
    components.TooltipComponent,
    components.LegendComponent,
    components.VisualMapComponent,
    components.GraphicComponent,
    renderers.CanvasRenderer,
  ])
  return core
})

export type ChartOption = echarts.EChartsCoreOption

/** 主题色以 r,g,b 逗号串注入文档根节点(--n-*-color),拼接为 rgb() 供 echarts 使用 */
export function cssRgb(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value ? `rgb(${value})` : fallback
}

/** 完整色值 CSS 变量(--c-*) */
export function cssVar(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

/** 轴线/网格通用配色(跟随明暗主题) */
export function chartAxisColors() {
  return {
    axisColor: cssVar('--c-secondary', '#8a8f99'),
    splitColor: cssVar('--c-line', '#e5e7eb'),
  }
}

/**
 * 空图表骨架:无数据时保留坐标系框架(轴线/网格线随主题)+ 居中「暂无数据」提示。
 * y 轴强制 0-100 刻度(value 轴无数据时不生成刻度区间,splitLine 一条都不画会整块空白)。
 * 各屏 buildOption 的无数据分支统一返回此 option。
 */
export function emptyChartOption(): ChartOption {
  const { axisColor, splitColor } = chartAxisColors()
  return {
    grid: { left: 8, right: 8, top: 24, bottom: 0, containLabel: true },
    xAxis: {
      type: 'category',
      data: [],
      axisTick: { show: false },
      axisLine: { lineStyle: { color: splitColor } },
      axisLabel: { color: axisColor },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLabel: { color: axisColor, formatter: () => '' },
      splitLine: { lineStyle: { color: splitColor, type: 'dashed' } },
    },
    series: [],
    graphic: [
      {
        type: 'text',
        left: 'center',
        top: 'middle',
        style: { text: '暂无数据', fill: axisColor, fontSize: 13 },
      },
    ],
  }
}

/**
 * echarts 实例生命周期(三屏共用):
 * - render():容器就绪时惰性 init + setOption(骨架屏期间容器 display:none,
 *   由调用方在数据就绪后 nextTick(render);render 内部有 el 守卫);
 * - ResizeObserver 容器自适应(移动端断点切换/面板伸缩自动 resize);
 * - 明暗主题切换后 nextTick 重绘(vueuse 在 post 阶段才更新 html.dark,
 *   同步重绘会读到上一主题的 --c-* 变量);
 * - 主题色配置变更后同样 nextTick 重绘(--n-* 随主题色注入,需重读);
 * - 卸载自动 dispose。
 * 已知缺口:主题联动 watch 仅覆盖 darkMode/themeConfig;后端下发的 remoteTheme 主色若在
 * 页面挂载前的 boot 脚本阶段生效,不会触发重绘(理论缺口,如需覆盖应 watch 生效配置)。
 * 必须在组件 setup 顶层同步调用(依赖 onUnmounted 与组件 scope)。
 */
export function useChart(buildOption: () => ChartOption) {
  const el = ref<HTMLElement>()
  let chart: echarts.ECharts | undefined
  let observer: ResizeObserver | undefined
  let boundEl: HTMLElement | undefined
  let disposed = false

  async function render() {
    if (!el.value) return
    // 首次渲染拉 echarts;期间组件可能卸载或容器重建,await 后逐一复查。
    // 加载失败(如发版后旧 chunk 404)静默留空:缓存已清,下次 render(轮询/主题切换)自动重试,
    // 吞掉 rejection 避免调用侧 void render() 产生 unhandled rejection。
    const echarts = await ensureEcharts().catch(() => null)
    if (!echarts || disposed || !el.value) return
    // v-if 重建容器:旧实例仍绑在脱离文档的旧 DOM 上,销毁后重新 init
    if (chart && boundEl !== el.value) dispose()
    if (!chart) {
      chart = echarts.init(el.value)
      boundEl = el.value
      observer = new ResizeObserver(() => chart?.resize())
      observer.observe(el.value)
    }
    chart.setOption(buildOption(), { notMerge: true })
  }

  function dispose() {
    observer?.disconnect()
    observer = undefined
    chart?.dispose()
    chart = undefined
    boundEl = undefined
  }

  const { darkMode, themeConfig } = useTheme()
  watch([darkMode, themeConfig], () => nextTick(() => void render()))
  onUnmounted(() => {
    disposed = true
    dispose()
  })

  return { el, render, dispose }
}
