import { h, ref } from 'vue'
import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useOperate, useTable } from '@repo/hooks'
import AuditDetailDrawer from './AuditDetailDrawer.vue'
import {
  useStore,
  actionMap,
  resultMap,
  actionFilterOptions,
  disableFutureDate,
  normalizeDateRange,
  resourceTypeLabel,
  formatDateTime,
  type AuditAction,
  type AuditLogData,
} from './useStore'

export function useController() {
  const { request } = useStore()

  /** 详情抽屉:当前查看的日志 + 显隐 */
  const detailShow = ref(false)
  const activeLog = ref<AuditLogData | null>(null)

  function openDetail(row: AuditLogData) {
    activeLog.value = row
    detailShow.value = true
  }

  /** 行操作:查看审计事件详情 */
  const selectActions: LmSelectAction<AuditLogData>[] = [
    { title: '详情', onClick: openDetail },
  ]

  /** 操作者展示名:用户名快照优先,缺失回退 #id,系统占位 */
  function actorLabel(row: AuditLogData): string {
    if (row.actor_name) return row.actor_name
    return '系统'
  }

  /** 操作/结果标签(未知枚举回退 default + 原串) */
  function tagRender(conf: { label: string; type: 'primary' | 'info' | 'success' | 'warning' | 'error' | 'default' } | undefined, fallback: string) {
    const resolved = conf ?? { label: fallback, type: 'default' as const }
    return h(
      NTag,
      { size: 'small', bordered: false, type: resolved.type },
      { default: () => resolved.label },
    )
  }

  const { LmTable, LmPage, LmSearch, LmRefresh, param, search, hasActiveQuery, reset } = useTable<
    AuditLogData,
    {
      action: AuditAction | ''
      resource_id: string
      dateRange: [number, number] | null
    }
  >({
    request,
    param: { action: '', resource_id: '', dateRange: null },
    columns: [
      {
        title: '时间',
        key: 'created_at',
        minWidth: 170,
        hideable: false,
        render: (row) => formatDateTime(row.created_at),
      },
      {
        title: '操作者',
        key: 'actor_name',
        minWidth: 160,
        render: (row) =>
          h('div', { class: 'flex items-center gap-[.5rem]' }, [
            h('span', actorLabel(row)),
          ]),
      },
      {
        title: '操作',
        key: 'action',
        minWidth: 90,
        render: (row) => tagRender(actionMap[row.action], row.action),
      },
      {
        title: '资源',
        key: 'resource_type',
        minWidth: 200,
        render: (row) =>
          h('div', { class: 'flex items-center gap-[.5rem] min-w-0' }, [
            row.resource_type
              ? h(
                  NTag,
                  { type: 'primary', size: 'small', bordered: false },
                  { default: () => resourceTypeLabel(row.resource_type) },
                )
              : null,
            h('span', { class: 'truncate' }, row.resource_id ?? '--'),
          ]),
      },
      {
        title: '结果',
        key: 'result',
        minWidth: 90,
        render: (row) => tagRender(resultMap[row.result], row.result),
      },
      useOperate<AuditLogData>(selectActions, { width: 70 }),
    ],
    rowKey: 'id',
    config: {
      // remote 模式:分页与筛选由后端完成(本租户作用域硬绑)
      pagination: { pageSize: 20 },
      tableProps: { flexHeight: false },
    },
  })

  /** 操作筛选变更:回第 1 页并请求 */
  function onActionChange(value: AuditAction | '' | null) {
    param.action = value ?? ''
    void search()
  }

  /** 日期区间变更:归一化为整天范围后回第 1 页并请求 */
  function onDateChange(value: [number, number] | null) {
    param.dateRange = normalizeDateRange(value)
    void search()
  }

  /** 清空筛选:重置表格查询 */
  function resetFilters() {
    void reset()
  }

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmSearch,
    LmRefresh,
    AuditDetailDrawer,
    param,
    actionFilterOptions,
    disableFutureDate,
    onActionChange,
    onDateChange,
    hasActiveQuery,
    resetFilters,
    detailShow,
    activeLog,
  }
}
