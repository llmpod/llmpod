import { onBeforeUnmount } from 'vue'
import { NSwitch } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useEntryAction, message } from '@repo/hooks'
import PackageForm from './PackageForm.vue'
import { useStore, packageTypeMap, type Package } from './useStore'

export function useController() {
  const { request, remove, subscribe } = useStore()

  /** 状态开关:本地切换并提示(静态) */
  function onToggleEnabled(row: Package, value: boolean) {
    row.enabled = value
    message.success(value ? `已上架:${row.name}` : `已下架:${row.name}`)
  }

  /** 删除:确认后移除记录 */
  async function onRemove(row: Package) {
    await remove(row.id)
    message.success(`已删除:${row.name}`)
  }

  /** 行操作:编辑 / 删除 */
  const selectActions: LmSelectAction<Package>[] = [
    { title: '编辑', onClick: (row) => openEdit(row) },
    { title: '删除', danger: true, onClick: onRemove },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, refresh } = useTable<Package>({
    request,
    columns: [
      { title: '名称', key: 'name', minWidth: 120, hideable: false },
      {
        title: '类型',
        key: 'type',
        width: 90,
        render: (row) => packageTypeMap[row.type],
      },
      {
        title: '价格',
        key: 'price',
        width: 110,
        render: (row) => `¥${row.price}/${row.type === 'monthly' ? '月' : '年'}`,
      },
      { title: 'Token', key: 'tokenQuota', width: 110 },
      { title: 'QPS', key: 'qps', width: 90 },
      { title: '并发', key: 'concurrency', width: 90 },
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
      { title: '订阅数', key: 'subscribers', width: 90 },
      useOperate<Package>(selectActions, { width: 120 }),
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

  /** 新建套餐弹窗 */
  function openCreate() {
    useDialog({
      title: '新建套餐',
      component: PackageForm,
      area: 45,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑套餐弹窗:回填当前行 */
  function openEdit(row: Package) {
    useDialog({
      title: '编辑套餐',
      component: PackageForm,
      area: 45,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  // 全局搜索「新建套餐」:?action=create 进入时自动打开新建弹窗
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
