import { onBeforeUnmount, watch } from 'vue'
import { NTag } from 'naive-ui'
import { useRoute, useRouter } from 'vue-router'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { message, useConfirm, useDialog, useEntryAction, useOperate, useTable } from '@repo/hooks'
import PoolForm from './PoolForm.vue'
import { useStore, clusterStatusMap, type NodePoolRecord } from './useStore'

export function useController() {
  const { request, toggleStatus, removePool, subscribe } = useStore()

  /** 顶部提示图标:public/icons/node-list/alert.svg,mask 渲染跟随 alert 图标配色 */
  const alertIcon = (() => {
    const src = `${import.meta.env.BASE_URL}icons/node-list/alert.svg`
    return {
      backgroundColor: 'currentColor',
      mask: `url(${src}) center/contain no-repeat`,
      WebkitMask: `url(${src}) center/contain no-repeat`,
    }
  })()

  /** 详情视图:非空时页面由表格切换为拓扑(记录用于定位初始集群) */
  const topologyPool = ref<NodePoolRecord | null>(null)

  function openTopology(row: NodePoolRecord) {
    topologyPool.value = row
  }
  function closeTopology() {
    topologyPool.value = null
  }

  /** 创建集群弹窗 */
  function openCreate() {
    useDialog({
      title: '创建集群',
      component: PoolForm,
      area: 50,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑集群弹窗(类型创建后不可改,表单内禁用) */
  function openEdit(row: NodePoolRecord) {
    useDialog({
      title: '编辑集群',
      component: PoolForm,
      area: 50,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  /** 启用/停用:结果文案由切换后状态决定 */
  async function onToggleStatus(row: NodePoolRecord) {
    const status = await toggleStatus(row)
    message.success(status === 'inactive' ? `已停用:${row.name}` : `已启用:${row.name}`)
  }

  /** 删除:二次确认后调用接口(集群下仍有节点时会被后端拒绝) */
  async function handleRemove(row: NodePoolRecord) {
    try {
      await useConfirm({
        title: '删除集群',
        content: `确定删除集群「${row.name}」吗?删除后不可恢复。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await removePool(row.id)
      message.success(`已删除:${row.name}`)
    }
    catch {
      // 取消,流程已打断
    }
  }

  /** 行操作:详情 / 编辑 / 停用(启用) / 删除 */
  const selectActions: LmSelectAction<NodePoolRecord>[] = [
    { title: '详情', onClick: openTopology },
    { title: '编辑', onClick: openEdit },
    {
      title: '停用',
      danger: true,
      show: (row) => row.status === 'active',
      onClick: onToggleStatus,
    },
    {
      title: '启用',
      show: (row) => row.status === 'inactive',
      onClick: onToggleStatus,
    },
    { title: '删除', danger: true, onClick: handleRemove },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, refresh, data } = useTable<NodePoolRecord>({
    request,
    columns: [
      {
        title: '集群名称',
        key: 'name',
        minWidth: 140,
        hideable: false,
        render: (row) =>
          h(
            'span',
            { class: 'text-primary cursor-pointer hover:opacity-[0.9]', onClick: () => openTopology(row) },
            row.name,
          ),
      },
      {
        title: '类型',
        key: 'type',
        width: 120,
        render: (row) =>
          h(
            NTag,
            { size: 'small', bordered: false, type: row.type === 'k8s' ? 'primary' : 'info' },
            { default: () => (row.type === 'k8s' ? 'Kubernetes' : 'Docker') },
          ),
      },
      { title: '节点数', key: 'nodeCount', width: 90 },
      { title: 'GPU总量', key: 'gpuTotal', width: 90 },
      {
        title: '状态',
        key: 'status',
        width: 100,
        render: (row) => {
          const conf = clusterStatusMap[row.status] ?? { label: row.status, type: 'default' }
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
      {
        title: '描述',
        key: 'description',
        minWidth: 140,
        render: (row) => row.description || '—',
      },
      useOperate<NodePoolRecord>(selectActions, { width: 170 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
      request: { pageSizeField: 'page_size' },
    },
  })

  // 订阅 store 变更:新增/编辑/状态切换/删除完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  // 拓扑直达:?topology=first 展开首行(部署向导「查看集群管理」);?topology=<clusterId> 展开指定
  // 集群(运营大屏 GPU 集群卡跳转)。首次数据加载后按参数定位,随即摘除 query,避免刷新/后退重复触发
  const route = useRoute()
  const router = useRouter()
  const topologyQuery = route.query.topology
  /** 拓扑直达待定:?topology= 进入时首屏数据在途,以骨架占位替代表格,避免先闪列表再切拓扑 */
  const entryPending = ref(typeof topologyQuery === 'string' && topologyQuery !== '')
  if (entryPending.value) {
    const stop = watch(data, (rows) => {
      stop()
      const target =
        topologyQuery === 'first' && rows.length
          ? rows[0]
          : rows.find((row) => String(row.id) === topologyQuery)
      if (target) openTopology(target)
      entryPending.value = false
      void router.replace({ query: {} })
    })
    onBeforeUnmount(stop)
  }

  // 全局搜索「创建集群」:?action=create 进入时自动打开新建弹窗
  useEntryAction({ create: openCreate })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    alertIcon,
    topologyPool,
    entryPending,
    openCreate,
    closeTopology,
  }
}
