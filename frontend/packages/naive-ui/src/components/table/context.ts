import type { ComputedRef, InjectionKey, Ref, ShallowRef } from 'vue'
import { inject, provide } from 'vue'
import type { DataTableColumn, DataTableRowData } from 'naive-ui'

/**
 * LmTable 上下文契约 —— 组件消费侧最小集。
 *
 * - 由 `useTable`(hooks 层) 创建并 `provide`；子组件(LmTablePage/Refresh/Column/Batch)经 `inject` 共享。
 * - 字段全部为 Ref/ShallowRef/ComputedRef，组件读 `.value` 拿响应式数据。
 * - 不包含 TableConfig / request / param / sorter / filters 等业务概念(那些是 hooks 层内部状态)。
 */
export interface LmTableContext<Row extends DataTableRowData = DataTableRowData> {
  // === 数据/加载 ===
  data: ShallowRef<Row[]>
  loading: Ref<boolean>

  // === 选择(checkedRows 由 NDataTable 的 update:checked-row-keys 第二参提供) ===
  checkedRowKeys: Ref<Array<string | number>>
  checkedRows: ShallowRef<Row[]>
  clearSelection: () => void
  /** 行 key 提取函数(hooks 层归一后提供;组件用于全选/反选计算) */
  rowKey: (row: Row) => string | number
  /** 受控设置选中行(仅传 keys,rows 按当前 data + rowKey 自动推导) */
  setSelection: (keys: Array<string | number>) => void

  // === 分页(供 LmTablePage 消费) ===
  page: Ref<number>
  pageSize: Ref<number>
  total: Ref<number>
  /** 切页由 LmTablePage 触发，转发给上下文(hooks 层据此重新请求) */
  onPageChange: (page: number) => void
  /** 修改每页条数(hooks 层负责重置到第 1 页) */
  onPageSizeChange: (pageSize: number) => void

  // === 刷新(供 LmTableRefresh 消费；保留当前查询条件与页码) ===
  refresh: (options?: { keepPage?: boolean }) => Promise<void> | void

  // === 列配置(供 LmTableColumn 消费) ===
  /** 调用方原始列(含不可隐藏列)，LmTableColumn 用它生成显隐列表 */
  rawColumns: ComputedRef<DataTableColumn<Row>[]>
  columnVisibleMap: Ref<Record<string, boolean>>
  setColumnVisible: (key: string, visible: boolean) => void
  resetColumnSetting: () => void
  /** 持久化 key；为 undefined 表示不持久化 */
  columnStorageKey?: string
}

/** 跨泛型 InjectionKey：用 any 占位，组件内 inject 后按需断言为具体 Row */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const LM_TABLE_KEY: InjectionKey<LmTableContext<any>> = Symbol('lm-table')

/** inject 辅助：统一默认值 + 类型断言；无祖先 provide 时返回 undefined */
export function useLmTableContext<Row extends DataTableRowData = DataTableRowData>(): LmTableContext<Row> | undefined {
  return inject(LM_TABLE_KEY, undefined) as LmTableContext<Row> | undefined
}

/** provide 辅助 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function provideLmTableContext(ctx: LmTableContext<any>): void {
  provide(LM_TABLE_KEY, ctx)
}
