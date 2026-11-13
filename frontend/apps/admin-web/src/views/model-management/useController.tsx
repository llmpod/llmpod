import { onBeforeUnmount } from 'vue'
import { NEllipsis, NButton, NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useDialog, useOperate, useTable, useConfirm, useEntryAction, message } from '@repo/hooks'
import AddModelDialog from './add-model/AddModelDialog.vue'
import EditModelDialog from './EditModelDialog.vue'
import ModelNodesDialog from './ModelNodesDialog.vue'
import {
  formatDateTime,
  SOURCE_LABEL,
  TYPE_TAG,
  typeLabel,
  useStore,
  type ModelRecord,
} from './useStore'

/** 已下载节点数:优先后端下发的计数,缺失时回退 cached_nodes 的节点数;两者都没有返回 null(未知,展示 --) */
function cachedNodeCount(row: ModelRecord): number | null {
  if (typeof row.cached_node_count === 'number') return row.cached_node_count
  if (row.cached_nodes) return Object.keys(row.cached_nodes).length
  return null
}

export function useController() {
  const { request, remove, subscribe } = useStore()

  /** 添加模型弹窗(分步向导:模型源 → 模型信息 → 确认提交,底部按钮由组件内自定义) */
  function openAdd() {
    useDialog({
      title: '添加模型',
      component: AddModelDialog,
      area: 60,
      btn: false,
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑模型弹窗(基础信息 + 元数据修正,PATCH 语义) */
  function openEdit(row: ModelRecord) {
    useDialog({
      title: '编辑模型',
      component: EditModelDialog,
      area: 60,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  /** 删除:二次确认后调用接口,删除成功后由 store notify 联动表格刷新 */
  async function onRemove(row: ModelRecord) {
    try {
      await useConfirm({
        title: '删除模型',
        content: `确定删除模型「${row.name}」吗?删除后不可恢复。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await remove(row.id)
      message.success(`已删除:${row.name}`)
    }
    catch {
      // 取消,流程已打断
    }
  }

  /**
   * 已下载节点:打开节点副本列表(节点/路径/节点状态/模型状态/大小/最近校验时间),
   * 弹窗内可逐节点清理(仅移除记录或删除磁盘文件)。
   */
  function openNodes(row: ModelRecord) {
    useDialog({
      title: `已下载节点 · ${row.name}`,
      component: ModelNodesDialog,
      area: 62,
      btn: false,
      props: { modelId: row.id, modelName: row.name },
    })
  }

  /**
   * 新建部署:跳到部署列表子 tab 并自动打开新建部署向导,预选当前行模型。
   * action 用独立名 deploy(而非 create):本页内嵌在 deploy 路由的 management 子 tab,
   * 同路由 query 变化时本页的 useEntryAction({create: openAdd}) 也会看到 action,
   * 同名会在切 tab 卸载前被误消费(弹「添加模型」并清掉 query),独立键天然互不干扰。
   * 经 ?action=deploy&model= 由列表挂载前的入口动作消费,向导首帧直接渲染,不闪列表。
   */
  const router = useRouter()
  function onDeploy(row: ModelRecord) {
    void router.push({
      name: 'deploy',
      query: { tab: 'list', action: 'deploy', model: String(row.id) },
    })
  }

  /** 行操作:编辑 / 新建部署 / 删除 */
  const selectActions: LmSelectAction<ModelRecord>[] = [
    { title: '编辑', onClick: openEdit },
    { title: '新建部署', onClick: onDeploy },
    { title: '删除', danger: true, onClick: onRemove },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, refresh } = useTable<ModelRecord>({
    request,
    columns: [
      { title: '名称', key: 'name', minWidth: 160, hideable: false },
      {
        title: '来源',
        key: 'source',
        render: (row) => (
          <div class="flex flex-col leading-tight">
            <span>{row.source ? (SOURCE_LABEL[row.source] ?? row.source) : '—'}</span>
            {row.source_url && (
              <NEllipsis class="text-base text-tertiary">{row.source_url}</NEllipsis>
            )}
          </div>
        ),
      },
      {
        title: '类别',
        key: 'model_type',
        render: (row) => {
          const conf = TYPE_TAG[row.model_type]
          return h(
            NTag,
            {
              size: 'small',
              bordered: false,
              ...(conf ? { color: { color: conf.color, textColor: conf.textColor } } : {}),
            },
            { default: () => typeLabel(row.model_type) },
          )
        },
      },
      {
        title: '格式',
        key: 'format',
        render: (row) => row.format ?? '—',
      },
      {
        title: '大小',
        key: 'size_gb',
        render: (row) => (row.size_gb != null ? `${row.size_gb} GB` : '—'),
      },
      {
        // 已下载节点数:点击展开节点副本列表(可查看路径/状态/大小/校验时间并清理)
        title: '已下载节点',
        key: 'cached_node_count',
        minWidth: 110,
        render: (row) => {
          const count = cachedNodeCount(row)
          return (
            <NButton text type={count ? 'primary' : 'default'} onClick={() => openNodes(row)}>
              {count === null ? '--' : count ? `已下载节点：${count}` : '未下载'}
            </NButton>
          )
        },
      },
      {
        title: '更新时间',
        key: 'updated_at',
        render: (row) => formatDateTime(row.updated_at),
      },
      useOperate<ModelRecord>(selectActions, { width: 110 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      request: { pageSizeField: 'page_size' },
      tableProps: { flexHeight: false },
    },
  })

  // 订阅 store 变更:创建/更新完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  // 全局搜索「添加模型」:?action=create 进入时自动打开添加向导
  useEntryAction({ create: openAdd })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    openAdd,
  }
}
