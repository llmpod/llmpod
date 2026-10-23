/* eslint-disable @typescript-eslint/no-explicit-any */
import type { Component, ComputedRef, Ref, ShallowRef } from 'vue'
import type { DataTableColumn, DataTableInst, PaginationProps } from 'naive-ui'
import type { LmBatchAction } from '@repo/naive-ui'

/** 请求参数:分页字段 + 业务查询字段 */
export interface TableRequestParams {
  page: number
  pageSize: number
  [key: string]: any
}

/** 请求返回:列表数据 + 总数 + 透传附加信息 */
export interface TableRequestResult<Row = Record<string, any>, Other = unknown> {
  data: Row[]
  total: number
  other?: Other
}

/** 请求上下文:取消信号(透传给底层 http 配置即可中断请求) */
export interface TableRequestContext {
  signal: AbortSignal
}

/** 远程请求方法;ctx.signal 供取消上一次请求使用,不需要可忽略 */
export type TableRequest<
  Row = Record<string, any>,
  Params extends object = Record<string, any>,
  Other = unknown,
> = (
  params: Params & TableRequestParams,
  ctx?: TableRequestContext,
) => Promise<TableRequestResult<Row, Other>>

export interface RefreshOptions {
  /** 是否保留当前页码(默认 true) */
  keepPage?: boolean
}

/** 列显隐/持久化配置 */
export interface TableColumnSetting {
  enabled?: boolean
  /**
   * 持久化 key;缺省自动按 `llmpod:columns[:<platform>]:<路由名>` 生成
   * (platform 取 vite base,如 admin/tenant;路由名取 route.name)。
   * 同一路由挂多个带列设置的表格时,须显式指定不同 key 避免互相覆盖。
   */
  storageKey?: string
  resetable?: boolean
  draggable?: boolean
}

/** 行选择配置 */
export interface TableSelection {
  enabled?: boolean
  /** 翻页是否保留选择 */
  reserveOnPageChange?: boolean
  /** 请求完成后是否清空选择 */
  clearAfterRequest?: boolean
}

/** 请求字段映射与行为 */
export interface TableRequestConfig {
  pageField?: string
  pageSizeField?: string
  /** 查询/重置时是否回到第 1 页,默认 true */
  resetPageOnSearch?: boolean
  /** 新请求发起时是否取消上一次未完成请求,默认 true;request 透传 ctx.signal 后才真正中断 */
  cancelPrevious?: boolean
}

/** 内置搜索框(LmSearch)配置 */
export interface TableFilterConfig {
  /** 筛选模式:remote 远程搜索(默认)/ local 前端过滤(对全量数据本地过滤+分页,不发请求) */
  mode?: 'remote' | 'local'
  /** local 模式参与匹配的字段(任一命中即保留);remote 模式忽略 */
  fields?: string[]
  /** 防抖毫秒,0 = 仅回车/图标触发;默认 300 */
  debounce?: number
  /** local 模式全量请求的 pageSize;不传则请求不带分页字段(约定后端返回全量) */
  fetchPageSize?: number
}

/** 窄屏(≤767px)卡片模式配置:LmTable 移动端把列定义转 label-value 卡片列表 */
export interface TableCardConfig {
  /** 是否启用,默认 true;false 时窄屏保持 n-data-table(需自行配 scrollX) */
  enabled?: boolean
  /** 卡片主标题列 key;缺省取第一个数据列 */
  titleKey?: string
  /** 卡面可见字段列 key 白名单(建议 2–3 项);缺省取前 3 个数据列。
   * 其余候选列不丢弃,收进「展开 N 项」明细区 */
  fieldKeys?: string[]
  /** 强调列 key(如金额):提到卡首行右端,17px 等宽数字右对齐;缺省不渲染强调位 */
  emphasisKey?: string
  /** 标签列 key:卡首行渲染为中性灰标签(语义交给强调数字与边轨);缺省不渲染标签 */
  tagKey?: string
  /** 异常行判定:命中时卡片左侧出现 3px 边轨,true 等价 'error';
   * 缺省无边轨 —— 正常行不涂色,异常靠"多出来的东西"被看见 */
  alertWhen?: (row: any) => boolean | 'warning' | 'error'
  /** 卡片密度:card 记录卡(默认)/ compact 56px 紧凑行(扫读用,不渲染勾选与行操作) */
  density?: 'card' | 'compact'
}

/** 表格整体配置 */
export interface TableConfig {
  /** false 关闭分页;否则覆盖默认分页配置 */
  pagination?: false | (Partial<PaginationProps> & {
    page?: number
    pageSize?: number
    pageSizes?: number[]
  })
  /** 透传至 n-data-table 的属性 */
  tableProps?: Record<string, any>
  /** 默认查询参数(每次 reset 回到这里) */
  defaultParams?: Record<string, any>
  columnSetting?: TableColumnSetting
  selection?: TableSelection
  request?: TableRequestConfig
  /** 内置搜索框配置 */
  filter?: TableFilterConfig
  /** 窄屏卡片模式配置 */
  card?: TableCardConfig
}

/** 列插件:本质是 DataTableColumn(useCheckbox/useOperate 返回完整列定义) */
export type TableColumnPlugin<Row = Record<string, any>> = DataTableColumn<Row>

/**
 * 列定义扩展:在原生 DataTableColumn 基础上支持锁定标记。
 * hideable:false = 锁定列 —— 列设置面板中显示但 checkbox 禁用(始终勾选),列不可被隐藏。
 */
export type LmDataTableColumn<Row extends Record<string, any> = Record<string, any>> =
  DataTableColumn<Row> & {
    hideable?: boolean
  }

/** 扩展:刷新联动 / 批量操作 */
export type TableExtension =
  | { type: 'refreshList'; source: Ref<boolean> }
  | { type: 'batch'; actions: LmBatchAction[] }

export interface UseTableOptions<
  Row extends Record<string, any> = Record<string, any>,
  Params extends object = Record<string, any>,
  Other = unknown,
> {
  /** 远程数据请求方法 */
  request: TableRequest<Row, Params, Other>
  /** 列定义;支持原生 Naive UI 列、锁定列标记(hideable:false)与列插件 */
  columns: Array<LmDataTableColumn<Row> | TableColumnPlugin<Row>>
  /** 初始查询参数 */
  param?: Partial<Params>
  /** 表格配置 */
  config?: TableConfig
  /** 扩展:刷新联动、批量操作等 */
  extension?: TableExtension[]
  /** 是否在初始化后自动加载第一页,默认 true */
  immediate?: boolean
  /** 行唯一标识;不得使用数组下标 */
  rowKey?: keyof Row | ((row: Row) => string | number)
  /** 请求错误回调 */
  onError?: (error: unknown) => void
}

export interface UseTableReturn<
  Row extends Record<string, any> = Record<string, any>,
  Params extends object = Record<string, any>,
  Other = unknown,
> {
  LmTable: Component
  LmPage: Component
  LmRefresh: Component
  LmColumn: Component
  LmBatch: Component
  LmSearch: Component

  /** 底层 n-data-table 实例 */
  tableRef: Ref<DataTableInst | null>

  /** 查询参数(reactive,可直接修改) */
  param: Params
  /** 合并默认后的完整配置 */
  config: TableConfig

  data: ShallowRef<Row[]>
  total: Ref<number>
  loading: Ref<boolean>
  /** 是否存在生效的查询条件(搜索关键字/列筛选/param 偏离初始值);用于空态区分 */
  hasActiveQuery: ComputedRef<boolean>
  other: ShallowRef<Other | undefined>

  selectedKeys: Ref<Array<string | number>>
  selectedRows: ShallowRef<Row[]>

  refresh: (options?: RefreshOptions) => Promise<void>
  reset: () => Promise<void>
  search: (params?: Partial<Params>) => Promise<void>
  setParam: (params: Partial<Params>) => void
  setData: (data: Row[], total?: number) => void
  clearSelection: () => void
}
