import { onBeforeUnmount } from 'vue'
import { NSwitch, NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useConfirm, useEntryAction, message } from '@repo/hooks'
import EndpointForm from './EndpointForm.vue'
import {
  useStore,
  healthStatusMap,
  type EndpointData,
} from './useStore'

export function useController() {
  const { request, toggleEnabled, remove, subscribe } = useStore()

  /** 删除：二次确认 */
  async function onRemove(row: EndpointData) {
    try {
      await useConfirm({
        title: '删除端点',
        content: `确定删除端点「${row.name}」吗？删除后不可恢复。若已有已发布的模型挂载了该端点，删除后这些模型将失去该上游，可能导致调用失败。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await remove(row.id)
      message.success(`已删除：${row.name}`)
    } catch {
      // 取消
    }
  }

  /** 切换中的行 id：确认弹窗与接口请求期间置 loading，防重复点击 */
  const togglingIds = ref(new Set<number>())

  /** 切换启用状态（二次确认）：停用即时退出数据面候选集，需说明影响面 */
  async function onToggleEnabled(row: EndpointData) {
    if (togglingIds.value.has(row.id)) return
    togglingIds.value.add(row.id)
    const action = row.enabled ? '停用' : '启用'
    const content = row.enabled
      ? `确定停用端点「${row.name}」吗？停用后该端点立即退出调用候选集。若已有已发布的模型挂载了该端点，这些模型将失去该上游，可能导致调用失败。记录保留，可随时重新启用。`
      : `确定启用端点「${row.name}」吗？`
    try {
      await useConfirm({
        title: `${action}端点`,
        content,
        confirmText: action,
        cancelText: '取消',
      })
      const enabled = await toggleEnabled(row)
      message.success(enabled ? `已启用：${row.name}` : `已停用：${row.name}`)
    } catch {
      // 取消
    } finally {
      togglingIds.value.delete(row.id)
    }
  }

  /** 行操作：编辑 / 删除（启停由启用列 switch 承接） */
  const selectActions: LmSelectAction<EndpointData>[] = [
    { title: '编辑', onClick: (row) => openEdit(row as EndpointData) },
    { title: '删除', danger: true, onClick: (row) => onRemove(row as EndpointData) },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, LmSearch, refresh, hasActiveQuery, reset } =
    useTable<EndpointData, { keyword: string }>({
      request,
      param: { keyword: '' },
      columns: [
        { title: '名称', key: 'name', minWidth: 140, hideable: false },
        {
          title: '协议',
          key: 'protocol',
          width: 100,
          render: (row) => row.protocol.toUpperCase(),
        },
        { title: '转发基址', key: 'api_base', minWidth: 200, ellipsis: { tooltip: true } },
        { title: '上游模型', key: 'served_model', minWidth: 120, ellipsis: { tooltip: true } },
        {
          title: '供应商',
          key: 'provider_name',
          width: 120,
          // 供应商空=外部登记端点，与挂载下拉口径一致
          render: (row) => row.provider_name ?? '外部',
        },
        {
          title: '健康',
          key: 'health_status',
          width: 90,
          render: (row) => {
            const conf = healthStatusMap[row.health_status]
              ?? { label: row.health_status, type: 'default' }
            return h(
              NTag,
              { type: conf.type, size: 'small', bordered: false },
              { default: () => conf.label },
            )
          },
        },
        {
          title: '启用',
          key: 'enabled',
          width: 80,
          render: (row) =>
            h(NSwitch, {
              value: row.enabled,
              size: 'small',
              loading: togglingIds.value.has(row.id),
              'onUpdate:value': () => onToggleEnabled(row),
            }),
        },
        useOperate<EndpointData>(selectActions, { width: 120 }),
      ],
      rowKey: 'id',
      config: {
        pagination: { pageSize: 10 },
        tableProps: { flexHeight: false },
        filter: {
          mode: 'local',
          fields: ['name', 'served_model', 'provider_name', 'api_base'],
        },
        columnSetting: { storageKey: 'llmpod:admin:model-release:endpoints' },
      },
    })

  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 新建端点弹窗 */
  function openCreate() {
    useDialog({
      title: '接入端点',
      component: EndpointForm,
      area: 48,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑端点弹窗 */
  function openEdit(row: EndpointData) {
    useDialog({
      title: '编辑端点',
      component: EndpointForm,
      props: { initial: row },
      area: 48,
      btn: { confirmText: '保存', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  useEntryAction({ create: openCreate })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    hasActiveQuery,
    reset,
    openCreate,
  }
}