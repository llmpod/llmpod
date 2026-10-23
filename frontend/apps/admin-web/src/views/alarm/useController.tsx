import { onBeforeUnmount } from 'vue'
import { NSwitch, NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useEntryAction, message } from '@repo/hooks'
import AlarmForm from './AlarmForm.vue'
import { useStore, alarmMetricMap, alarmChannelMap, type AlarmRule } from './useStore'

export function useController() {
  const { request, remove, subscribe } = useStore()

  /** 状态开关:本地切换并提示(静态) */
  function onToggleEnabled(row: AlarmRule, value: boolean) {
    row.enabled = value
    message.success(value ? `已启用:${row.name}` : `已停用:${row.name}`)
  }

  /** 删除:确认后移除记录 */
  async function onRemove(row: AlarmRule) {
    await remove(row.id)
    message.success(`已删除:${row.name}`)
  }

  /** 行操作:编辑 / 删除 */
  const selectActions: LmSelectAction<AlarmRule>[] = [
    { title: '编辑', onClick: (row) => openEdit(row) },
    { title: '删除', danger: true, onClick: onRemove },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, refresh } = useTable<AlarmRule>({
    request,
    columns: [
      { title: '规则名称', key: 'name', minWidth: 140, hideable: false },
      {
        title: '指标',
        key: 'metric',
        width: 110,
        render: (row) => alarmMetricMap[row.metric],
      },
      {
        title: '触发条件',
        key: 'threshold',
        minWidth: 160,
        render: (row) => `${alarmMetricMap[row.metric]} ${row.comparator} ¥${row.threshold}`,
      },
      {
        title: '通知方式',
        key: 'channels',
        width: 140,
        render: (row) =>
          row.channels.map((channel) =>
            h(
              NTag,
              { size: 'small', bordered: false, type: 'primary', class: 'mr-[.25rem]' },
              { default: () => alarmChannelMap[channel] },
            ),
          ),
      },
      {
        title: '状态',
        key: 'enabled',
        width: 90,
        render: (row) =>
          h(NSwitch, {
            value: row.enabled,
            size: 'small',
            'onUpdate:value': (value: boolean) => onToggleEnabled(row, value),
          }),
      },
      useOperate<AlarmRule>(selectActions, { width: 120 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
    },
  })

  // 订阅 store 变更:新增/编辑/删除完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 新建告警规则弹窗 */
  function openCreate() {
    useDialog({
      title: '新建告警规则',
      component: AlarmForm,
      area: 45,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑告警规则弹窗:回填当前行 */
  function openEdit(row: AlarmRule) {
    useDialog({
      title: '编辑告警规则',
      component: AlarmForm,
      area: 45,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  // 全局搜索「新建告警规则」:?action=create 进入时自动打开新建弹窗
  useEntryAction({ create: openCreate })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    openCreate,
  }
}
