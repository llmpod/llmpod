import type { DataTableRowData } from 'naive-ui'

/** 批量操作项(LmTableBatch) */
export interface LmBatchAction {
  label: string
  value: string
  disabled?: boolean
  /** 危险操作(红色文字标记) */
  danger?: boolean
}

/** 行操作项(LmTableSelect) */
export interface LmSelectAction<Row extends DataTableRowData = DataTableRowData> {
  title: string
  key?: string
  /** 是否显示该项，默认 true；支持按行数据动态判断 */
  show?: boolean | ((row: Row) => boolean)
  /** 是否禁用，支持按行数据动态判断 */
  disabled?: boolean | ((row: Row) => boolean)
  /** 危险操作:渲染为红色(error)文字按钮;需要二次确认时使用 beforeConfirm 自行处理 */
  danger?: boolean
  onClick?: (row: Row) => unknown
  /** 执行前拦截器，返回 false 阻断 onClick */
  beforeConfirm?: (row: Row) => boolean | Promise<boolean>
  /** 标记为"更多"入口:渲染为文字按钮"更多" + 下拉菜单,下拉内容取 children */
  more?: boolean
  /** more 项的下拉子操作(仅 more=true 时生效) */
  children?: LmSelectAction<Row>[]
}

/** 列持久化元数据(仅展示属性，不含 render/函数) */
export interface ColumnPersistenceMeta {
  visible?: boolean
  order?: number
  fixed?: 'left' | 'right' | false
  width?: number
}

export type ColumnPersistenceMap = Record<string, ColumnPersistenceMeta>

/** LmTableBatch 的 select 事件载荷 */
export interface LmTableBatchSelectPayload {
  action: string
  selectedKeys: Array<string | number>
  selectedRows: DataTableRowData[]
  refresh: (options?: { keepPage?: boolean }) => Promise<void> | void
}
