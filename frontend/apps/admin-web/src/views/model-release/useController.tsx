import { onBeforeUnmount, ref } from 'vue'
import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useConfirm, message } from '@repo/hooks'
import RouteForm from './components/RouteForm.vue'
import BindingDrawer from './components/BindingManage.vue'
import {
  useStore,
  routeStatusMap,
  modelTypeMap,
  formatPrice,
  type PublishData,
} from './useStore'

export function useController() {
  const { request, unpublish, republish, remove, subscribe } = useStore()

  /** 下架：二次确认 */
  async function onUnpublish(row: PublishData) {
    try {
      await useConfirm({
        title: '下架模型',
        content: `确定下架「${row.route.name}」吗？下架后租户将无法调用该模型。`,
        confirmText: '下架',
        cancelText: '取消',
      })
      await unpublish(row.route.id)
      message.success(`已下架：${row.route.name}`)
    } catch {
      // 取消
    }
  }

  /** 重新上架 */
  async function onRepublish(row: PublishData) {
    await republish(row.route.id)
    message.success(`已上架：${row.route.name}`)
  }

  /** 删除：二次确认（连带清成员与租户授权引用，不可恢复） */
  async function onRemove(row: PublishData) {
    try {
      await useConfirm({
        title: '删除路由',
        content: `确定删除「${row.route.name}」吗？将连带清除成员绑定与租户授权，不可恢复。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await remove(row.route.id)
      message.success(`已删除：${row.route.name}`)
    } catch {
      // 取消
    }
  }

  /** 行操作：供应商 / 编辑定价 / 下架(上架) / 删除 */
  const selectActions: LmSelectAction<PublishData>[] = [
    { title: '供应商', onClick: (row) => openBindings(row as PublishData) },
    { title: '编辑定价', onClick: (row) => openEdit(row as PublishData) },
    {
      title: '下架',
      danger: true,
      show: (row) => row.route.status === 'published',
      onClick: (row) => onUnpublish(row as PublishData),
    },
    {
      title: '上架',
      show: (row) => row.route.status === 'unpublished',
      onClick: (row) => onRepublish(row as PublishData),
    },
    { title: '删除', danger: true, onClick: (row) => onRemove(row as PublishData) },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, LmSearch, refresh, hasActiveQuery, reset } =
    useTable<PublishData, { keyword: string }>({
      request,
      param: { keyword: '' },
      columns: [
        { title: '对外模型名', key: 'route.name', minWidth: 140, hideable: false,
          render: (row) => row.route.name },
        {
          title: '类型',
          key: 'route.model_type',
          width: 100,
          render: (row) => modelTypeMap[row.route.model_type] ?? row.route.model_type,
        },
        {
          title: '状态',
          key: 'route.status',
          width: 100,
          render: (row) => {
            const conf = routeStatusMap[row.route.status]
              ?? { label: row.route.status, type: 'default' }
            return h(
              NTag,
              { type: conf.type, size: 'small', bordered: false },
              { default: () => conf.label },
            )
          },
        },
        {
          title: '输入定价',
          key: 'input_price',
          width: 110,
          render: (row) => formatPrice(row.route.input_price_per_mtok),
        },
        {
          title: '输出定价',
          key: 'output_price',
          width: 110,
          render: (row) => formatPrice(row.route.output_price_per_mtok),
        },
        {
          title: '成员数',
          key: 'bindings.length',
          width: 80,
          render: (row) => row.bindings.length,
        },
        {
          title: '自动切换',
          key: 'failover',
          width: 100,
          render: (row) =>
            row.route.failover_enabled
              ? `开 · ${row.route.failover_max_retries} 次`
              : '关',
        },
        useOperate<PublishData>(selectActions, { width: 170 }),
      ],
      rowKey: (row) => row.route.id,
      config: {
        pagination: { pageSize: 10 },
        tableProps: { flexHeight: false },
      },
    })

  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 新建发布弹窗(手动创建路由,即建即上架;创建后可在成员管理挂端点) */
  function openCreate() {
    useDialog({
      title: '新建发布',
      component: RouteForm,
      area: 45,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑定价弹窗 */
  function openEdit(row: PublishData) {
    useDialog({
      title: '编辑路由定价',
      component: RouteForm,
      props: { initial: row.route },
      area: 45,
      btn: { confirmText: '保存', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 供应商抽屉:当前操作的路由 + 显隐 */
  const activePublish = ref<PublishData | null>(null)
  const bindingDrawerShow = ref(false)

  /** 打开供应商抽屉 */
  function openBindings(row: PublishData) {
    activePublish.value = row
    bindingDrawerShow.value = true
  }

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
    openBindings,
    activePublish,
    bindingDrawerShow,
  }
}