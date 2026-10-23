import { h, type Ref } from 'vue'
import type { DataTableRowData } from 'naive-ui'
import { LmTableSelect, resolveOperateWidth } from '@repo/naive-ui'
import type { LmBatchAction, LmSelectAction } from '@repo/naive-ui'
import type { TableColumnPlugin, TableExtension } from './table-types'

/**
 * 选择列插件。
 * @param options.type 'page'(默认,当前页) | 'all'(跨页保留)
 */
export function useCheckbox<Row extends DataTableRowData = DataTableRowData>(options?: {
  key?: keyof Row
  type?: 'page' | 'all'
  reserve?: boolean
}): TableColumnPlugin<Row> {
  void options
  return {
    type: 'selection',
    multiple: true,
  } as unknown as TableColumnPlugin<Row>
}

/**
 * 操作列插件。内部用 LmTableSelect 渲染行操作,并打 __lmOperate 标记
 * 供 LmTableColumn 过滤为不可隐藏列。
 *
 * 桌面全量平铺不收纳(收纳只属于窄屏卡片);列宽取声明值与平铺估宽的
 * max(estimateOperateWidth),按「2 项 + 更多」定的旧列宽自动被顶宽,业务侧不必逐页改。
 */
export function useOperate<Row extends DataTableRowData = DataTableRowData>(
  actions: LmSelectAction<Row>[],
  options?: {
    title?: string
    key?: string
    width?: number
    fixed?: 'left' | 'right'
  },
): TableColumnPlugin<Row> {
  const { title = '操作', key = 'operate', width = 120, fixed = 'right' } = options ?? {}
  // 静态 show(false 项不渲染,不占宽);函数式 show 按行变化,取全量标题估宽兜底
  const labels = actions
    .filter((a) => !a.more && a.show !== false)
    .map((a) => a.title)
  return {
    title,
    key,
    fixed,
    align: 'right',
    width: resolveOperateWidth(width, labels),
    __lmOperate: true,
    render: (row: Row) =>
      h(LmTableSelect, { row, actions: actions as unknown as LmSelectAction[] }),
  } as unknown as TableColumnPlugin<Row>
}

/** 刷新联动扩展:source 变 true 时触发 refresh */
export function useRefreshList(source: Ref<boolean>): TableExtension {
  return { type: 'refreshList', source }
}

/** 批量操作扩展:actions 注入短名 LmBatch */
export function useBatch(actions: LmBatchAction[]): TableExtension {
  return { type: 'batch', actions }
}
