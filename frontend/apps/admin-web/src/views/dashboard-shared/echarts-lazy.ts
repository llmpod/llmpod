import * as core from 'echarts/core'
import { BarChart, HeatmapChart, LineChart } from 'echarts/charts'
import {
  GraphicComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

/**
 * echarts 按需注册模块(三屏 dashboard 实际用到的图表/组件/渲染器)。
 *
 * 为什么是「本地模块 + 静态命名导入」而不是 `import('echarts/charts')` 后取
 * `charts.LineChart`:动态 import barrel 再做命名空间成员访问,打包器无法静态
 * 确定实际用到的图表,只能保守保留 barrel 全部导出的依赖链(Map/parseGeoJson
 * 等未用图表的 ~500KB 会一起进包)。本模块内全部为静态命名导入,tree-shaking
 * 可完整生效;调用方 `import('./echarts-lazy')` 依旧按需(动态)加载。
 */
core.use([
  LineChart,
  BarChart,
  HeatmapChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  VisualMapComponent,
  GraphicComponent,
  CanvasRenderer,
])

export default core
