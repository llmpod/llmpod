/* eslint-disable @typescript-eslint/no-explicit-any */
import type { DataTableColumn } from 'naive-ui'
import type { TableConfig, TableRequestParams } from './table-types'

/** 默认表格配置(对齐文档) */
export const defaultTableConfig: TableConfig = {
  pagination: {
    page: 1,
    pageSize: 20,
    pageSizes: [10, 20, 50, 100],
    showSizePicker: true,
    showQuickJumper: true,
  },
  tableProps: {
    striped: true,
    bordered: false,
    remote: true,
    flexHeight: true,
  },
  columnSetting: {
    enabled: true,
    resetable: true,
    draggable: false,
  },
  selection: {
    enabled: true,
    reserveOnPageChange: false,
    clearAfterRequest: false,
  },
  request: {
    pageField: 'page',
    pageSizeField: 'pageSize',
    resetPageOnSearch: true,
    cancelPrevious: true,
  },
  card: {
    enabled: true,
  },
}

/** 合并默认配置与用户配置(浅合并顶层,嵌套对象单独 merge) */
export function mergeConfig(userConfig?: TableConfig): TableConfig {
  if (!userConfig) return structuredClone(defaultTableConfig)
  const merged: TableConfig = { ...defaultTableConfig, ...userConfig }

  if (userConfig.pagination === false) {
    merged.pagination = false
  }
  else if (userConfig.pagination && defaultTableConfig.pagination) {
    merged.pagination = { ...defaultTableConfig.pagination, ...userConfig.pagination }
  }

  merged.columnSetting = { ...defaultTableConfig.columnSetting, ...userConfig.columnSetting }
  merged.selection = { ...defaultTableConfig.selection, ...userConfig.selection }
  merged.request = { ...defaultTableConfig.request, ...userConfig.request }
  merged.card = { ...defaultTableConfig.card, ...userConfig.card }
  // tableProps 同样逐键合并:部分覆盖(如仅 flexHeight)不丢 striped/remote 等默认;
  // 无条件重建也断开与模块级默认的引用,防运行时改 config.tableProps 污染全局
  merged.tableProps = { ...defaultTableConfig.tableProps, ...userConfig.tableProps }

  return merged
}

/** 列解析:插件已返回 DataTableColumn,此处仅类型归一 */
export function resolveColumns<Row>(columns: Array<DataTableColumn<Row>>): DataTableColumn<Row>[] {
  return columns
}

/** 数字或 'Npx' 字符串转像素数;其它形式(百分比等)返回 undefined */
function toPx(value: number | string): number | undefined {
  if (typeof value === 'number') return value > 0 ? value : undefined
  return /^\d+(\.\d+)?px$/.test(value.trim()) ? parseFloat(value) : undefined
}

/**
 * 生效列声明宽度(width/minWidth,取先声明者)求和,作为默认 scroll-x 下限:
 * 容器更窄时表格保持最小内容宽 → 横向滚动且滚动条可见。
 * selection/expand 列按 naive 默认宽 40 计(与 getNumberColWidth 一致)。
 * 存在无法估宽的列(弹性列/分组列/百分比宽)时返回 undefined —— 不注入,
 * 维持"窄容器列压缩"现状,需要横滚的场景由业务显式传 scrollX。
 */
export function sumColumnWidth<Row>(columns: DataTableColumn<Row>[]): number | undefined {
  let sum = 0
  for (const col of columns) {
    const c = col as unknown as Record<string, unknown>
    if (c.type === 'selection' || c.type === 'expand') {
      sum += (c.width as number | string | undefined) != null
        ? toPx(c.width as number | string) ?? 0
        : 40
      continue
    }
    if (Array.isArray(c.children)) return undefined
    const declared = (c.width ?? c.minWidth) as number | string | undefined
    const px = declared != null ? toPx(declared) : undefined
    if (px == null) return undefined
    sum += px
  }
  return sum
}

/**
 * 应用列显隐配置:过滤被用户隐藏的可隐藏列。
 * 不可隐藏列(selection/expand/操作列/锁定列/无 key 列)始终保留;
 * 锁定列(hideable:false)不受 visibleMap 影响,localStorage 脏数据也不生效。
 */
export function applyColumnSetting<Row>(
  columns: DataTableColumn<Row>[],
  visibleMap: Record<string, boolean>,
): DataTableColumn<Row>[] {
  return columns.filter((col) => {
    const c = col as unknown as Record<string, unknown>
    if (c.type === 'selection' || c.type === 'expand') return true
    // 操作列约定标记(由 useOperate 打标)
    if (c.__lmOperate === true) return true
    // 锁定列标记(hideable:false):不可隐藏
    if (c.hideable === false) return true
    const key = c.key
    if (key == null) return true
    return visibleMap[String(key)] !== false
  })
}

/** 按文档顺序合并请求参数;page/pageSize 为 undefined 时不附加分页字段(local 全量请求场景) */
export function buildParams<Params extends object>(options: {
  defaultParams?: Record<string, any>
  param: Params
  /** 页码;undefined 时不附加分页字段 */
  page?: number
  /** 每页条数;undefined 时不附加分页字段 */
  pageSize?: number
  sorter?: any
  filters?: any
  pageField: string
  pageSizeField: string
}): Params & TableRequestParams {
  const { defaultParams = {}, param, page, pageSize, sorter, filters, pageField, pageSizeField } = options
  const result: Record<string, any> = { ...defaultParams, ...param }
  if (page !== undefined) result[pageField] = page
  if (pageSize !== undefined) result[pageSizeField] = pageSize
  result.sorter = sorter
  result.filters = filters
  return result as unknown as Params & TableRequestParams
}

/** 空值归一:undefined / null / '' 均视为"未设置" */
function isEmptyValue(value: unknown): boolean {
  return value === undefined || value === null || value === ''
}

/**
 * 生效查询条件判定入参。
 * 契约:param 值仅支持标量(string/number/boolean 等),数组/对象值不参与本判定;
 * initialParam 需为初始化时的快照,不可与 param 共享可变引用。
 */
export interface ActiveQueryInput {
  /** 当前查询参数;未传时按空对象处理 */
  param?: Record<string, any>
  /** 初始查询参数(defaultParams + param 的合并结果) */
  initialParam: Record<string, any>
  /** local 模式前端过滤关键字 */
  filterKeyword: string
  /** n-data-table 列筛选值 */
  filters: Record<string, any> | null
}

/**
 * 是否存在生效的查询条件(搜索关键字 / 列筛选 / param 偏离初始值)。
 * 空态据此区分:"真无数据"展示新建引导,"筛选无结果"展示清空筛选引导。
 */
export function checkActiveQuery(input: ActiveQueryInput): boolean {
  const { param = {}, initialParam, filterKeyword, filters } = input
  if (filterKeyword.trim()) return true
  if (filters) {
    const hasActiveFilter = Object.values(filters).some((value) =>
      Array.isArray(value) ? value.length > 0 : !isEmptyValue(value),
    )
    if (hasActiveFilter) return true
  }
  const keys = new Set([...Object.keys(initialParam), ...Object.keys(param)])
  for (const key of keys) {
    const current = param[key]
    const initial = initialParam[key]
    if (isEmptyValue(current) && isEmptyValue(initial)) continue
    if (current !== initial) return true
  }
  return false
}
