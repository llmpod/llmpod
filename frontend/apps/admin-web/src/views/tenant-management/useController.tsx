import { onBeforeUnmount, onMounted } from 'vue'
import { NButton, NPopover, NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useConfirm, useEntryAction, message, buildPortalLoginUrl } from '@repo/hooks'
import TenantManagementForm from './TenantManagementForm.vue'
import TenantManagementDetail from './TenantManagementDetail.vue'
import TenantWalletAdjustForm from './TenantWalletAdjustForm.vue'
import {
  useStore,
  tenantStatusFilterOptions,
  tenantStatusMap,
  formatDateTime,
  formatDecimal,
  type Tenant,
} from './useStore'

export function useController() {
  const { request, toggleStatus, remove, subscribe, overviewCards, fetchOverview } = useStore()

  /** 顶部概览统计卡:进入页面拉取;写操作(新增/删除/启停)后随 subscribe 一并刷新 */
  const overviewLoading = ref(true)
  function loadOverview() {
    void fetchOverview()
      .catch(() => {})
      .finally(() => {
        overviewLoading.value = false
      })
  }
  onMounted(loadOverview)

  /** 详情弹窗:基本信息/成员列表 */
  function openDetail(row: Tenant) {
    useDialog({
      title: `${row.name} 详情`,
      component: TenantManagementDetail,
      area: 90,
      btn: false,
      props: { tenant: row },
      dialogProps: { maskClosable: false },
    })
  }

  /** 调额弹窗:人工 add/subtract/override 租户余额 */
  function openAdjust(row: Tenant) {
    useDialog({
      title: `调额 - ${row.name}`,
      component: TenantWalletAdjustForm,
      area: 40,
      props: { tenant: row },
      btn: { confirmText: '确认调额', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 启用/禁用:结果文案由切换后状态决定 */
  async function onToggleStatus(row: Tenant) {
    const status = await toggleStatus(row)
    message.success(status === 'disabled' ? `已禁用:${row.name}` : `已启用:${row.name}`)
  }

  /** 删除:二次确认后调用接口(后端级联清空该租户用户及其角色后直接删除) */
  async function onRemove(row: Tenant) {
    try {
      await useConfirm({
        title: '删除租户',
        content: `确定删除租户「${row.name}」吗?该租户下的用户及其角色将被一并清空,删除后不可恢复。`,
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

  /** 行操作:详情 / 编辑 / 调额 / 禁用(启用) / 删除 */
  const selectActions: LmSelectAction<Tenant>[] = [
    { title: '详情', onClick: (row) => openDetail(row as Tenant) },
    { title: '编辑', onClick: (row) => openEdit(row as Tenant) },
    { title: '调额', onClick: (row) => openAdjust(row as Tenant) },
    {
      title: '禁用',
      danger: true,
      show: (row) => row.status === 'active',
      onClick: onToggleStatus,
    },
    {
      title: '启用',
      show: (row) => row.status === 'disabled',
      onClick: onToggleStatus,
    },
    { title: '删除', danger: true, onClick: (row) => onRemove(row as Tenant) },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, LmSearch, param, search, refresh, hasActiveQuery, reset } = useTable<
    Tenant,
    { keyword: string; status: string }
  >({
    request,
    param: { keyword: '', status: '' },
    columns: [
      { title: '租户名', key: 'name', minWidth: 160, hideable: false },
      {
        title: '状态',
        key: 'status',
        width: 110,
        render: (row) => {
          const conf = tenantStatusMap[row.status] ?? { label: row.status, type: 'default' }
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
      {
        title: '禁用模型',
        key: 'blocked_models',
        width: 100,
        render: (row) => {
          const models = row.blocked_models ?? []
          // 无封禁显示占位符;有封禁显示「查看」,悬浮 popover 展示模型名 tag
          if (!models.length) return '--'
          return h(
            NPopover,
            { trigger: 'hover' },
            {
              trigger: () =>
                h(NButton, { text: true, type: 'primary', size: 'small' }, { default: () => '查看' }),
              default: () =>
                h(
                  'div',
                  { class: 'flex max-w-[24rem] flex-wrap gap-[.4rem]' },
                  models.map(m =>
                    h(
                      NTag,
                      { key: m.route_id, size: 'small', bordered: false },
                      { default: () => m.name },
                    ),
                  ),
                ),
            },
          )
        },
      },
      {
        title: '余额(元)',
        key: 'balance',
        minWidth: 150,
        render: (row) =>
          h('div', { class: 'flex flex-col text-xs' }, [
            // h('span', null, `总 ${formatDecimal(row.balance)}`),
            // h('span', { class: 'text-secondary' }, `冻结 ${formatDecimal(row.frozen_balance)}`),
            h('span', { class: 'text-secondary' }, `${formatDecimal(row.available)}`),
          ]),
      },
      {
        title: '创建时间',
        key: 'created_at',
        width: 160,
        render: (row) => formatDateTime(row.created_at),
      },
      {
        title: '更新时间',
        key: 'updated_at',
        width: 160,
        render: (row) => formatDateTime(row.updated_at),
      },
      useOperate<Tenant>(selectActions, { width: 200 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
      request: { pageSizeField: 'page_size' },
      // 窄屏卡片:租户名做主标题、余额提到卡首;每行可见 5 项行操作,
      // 由 LmTableSelect 按实测卡宽自动切片(前 2 项内联,其余沉进底部动作面板)
      card: {
        titleKey: 'name',
        tagKey: 'status',
        emphasisKey: 'balance',
        fieldKeys: ['blocked_models', 'created_at'],
        alertWhen: (row: Tenant) => (row.status === 'disabled' ? 'warning' : false),
      },
    },
  })

  // 订阅 store 变更:新增/编辑/状态切换/删除/调额完成后自动刷新表格与顶部统计卡
  const unsubscribe = subscribe(() => {
    void refresh()
    void fetchOverview().catch(() => {})
  })
  onBeforeUnmount(unsubscribe)

  /** 状态筛选变更:回第 1 页并请求(服务端按 status 过滤) */
  function onStatusChange(value: string | number) {
    param.status = String(value)
    void search()
  }

  /** 跳转租户登录:新标签页打开 portal 统一登录页(登录后按账号所属平台自动进入租户端) */
  function goTenantLogin() {
    window.open(buildPortalLoginUrl(), '_blank')
  }

  /** 新增租户弹窗 */
  function openCreate() {
    useDialog({
      title: '新增租户',
      component: TenantManagementForm,
      area: 40,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑租户弹窗:回填当前行 */
  function openEdit(row: Tenant) {
    useDialog({
      title: '编辑租户',
      component: TenantManagementForm,
      area: 40,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  // 全局搜索「新增租户」:?action=create 进入时自动打开新建弹窗
  useEntryAction({ create: openCreate })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    param,
    overviewCards,
    overviewLoading,
    tenantStatusFilterOptions,
    LmSearch,
    onStatusChange,
    goTenantLogin,
    hasActiveQuery,
    reset,
    openCreate,
  }
}
