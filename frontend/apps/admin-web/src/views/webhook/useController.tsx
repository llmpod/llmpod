import { onBeforeUnmount } from 'vue'
import { NSwitch, NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useEntryAction, message } from '@repo/hooks'
import WebhookForm from './WebhookForm.vue'
import { useStore, type Webhook } from './useStore'

export function useController() {
  const { request, test, subscribe } = useStore()

  /** 状态开关:本地切换并提示(静态) */
  function onToggleEnabled(row: Webhook, value: boolean) {
    row.enabled = value
    message.success(value ? `已启用:${row.name}` : `已停用:${row.name}`)
  }

  /** 测试:模拟发送一次测试事件 */
  async function onTest(row: Webhook) {
    await test(row.id)
    message.success(`测试事件已发送:${row.url}`)
  }

  /** 行操作:编辑 / 测试 */
  const selectActions: LmSelectAction<Webhook>[] = [
    { title: '编辑', onClick: (row) => openEdit(row) },
    { title: '测试', onClick: onTest },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, refresh } = useTable<Webhook>({
    request,
    columns: [
      { title: '名称', key: 'name', minWidth: 140, hideable: false },
      {
        title: '事件类型',
        key: 'event',
        width: 200,
        render: (row) =>
          h(NTag, { size: 'small', bordered: false, type: 'success' }, { default: () => row.event }),
      },
      { title: '目标URL', key: 'url', minWidth: 260 },
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
      { title: '最后触发', key: 'lastTriggeredAt', width: 170 },
      useOperate<Webhook>(selectActions, { width: 120 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
    },
  })

  // 订阅 store 变更:新增/编辑完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 新建 Webhook 弹窗 */
  function openCreate() {
    useDialog({
      title: '新建Webhook',
      component: WebhookForm,
      area: 45,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑 Webhook 弹窗:回填当前行 */
  function openEdit(row: Webhook) {
    useDialog({
      title: '编辑Webhook',
      component: WebhookForm,
      area: 45,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  // 全局搜索「新建 Webhook」:?action=create 进入时自动打开新建弹窗
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
