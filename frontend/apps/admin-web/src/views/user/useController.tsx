import { onBeforeUnmount, onMounted } from 'vue'
import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useConfirm, useEntryAction, message } from '@repo/hooks'
import { listRoles } from '@/api/access'
import { useAuthStore } from '@/store/auth'
import UserForm from './UserForm.vue'
import {
  useStore,
  userStatusMap,
  statusFilterOptions,
  formatDateTime,
  type UserData,
} from './useStore'

export function useController() {
  const { pageItems, total, request, toggleStatus, remove, subscribe } = useStore()
  const auth = useAuthStore()

  /** 是否当前登录用户本人的行:禁止禁用/启用/删除自己 */
  function isSelf(row: UserData) {
    return auth.userInfo?.user_id === row.id
  }

  /** 顶部统计卡片:总用户为服务端总数,其余为当前页统计(后端暂不支持分类计数) */
  const statCards = computed(() => {
    const list = pageItems.value
    return [
      { label: '总用户', value: String(total.value) },
      {
        label: '启用(本页)',
        value: String(list.filter((r) => r.status === 'active').length),
        tone: 'success' as const,
      },
      {
        label: '禁用(本页)',
        value: String(list.filter((r) => r.status === 'disabled').length),
        tone: 'error' as const,
      },
      {
        label: '绑定角色(本页)',
        value: `${new Set(list.flatMap((r) => r.role_ids)).size}个`,
      },
    ]
  })

  /** 当前操作的用户 + 详情抽屉显隐 */
  const activeUser = ref<UserData | null>(null)
  const detailDrawerShow = ref(false)

  /** 角色 ID → 名称映射(进页面拉一次,供列表角色列渲染) */
  const roleNames = ref<Map<number, string>>(new Map())
  onMounted(async () => {
    const roles = await listRoles()
    roleNames.value = new Map(roles.map((role) => [role.id, role.name]))
  })

  function openDetail(row: UserData) {
    activeUser.value = row
    detailDrawerShow.value = true
  }

  /** 启用/禁用:结果文案由切换后状态决定 */
  async function onToggleStatus(row: UserData) {
    const status = await toggleStatus(row)
    message.success(status === 'disabled' ? `已禁用:${row.username}` : `已启用:${row.username}`)
  }

  /** 删除:二次确认后调用接口 */
  async function onRemove(row: UserData) {
    try {
      await useConfirm({
        title: '删除用户',
        content: `确定删除用户「${row.username}」吗?删除后不可恢复。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await remove(row.id)
      message.success(`已删除:${row.username}`)
    } catch {
      // 取消,流程已打断
    }
  }

  /** 行操作:详情 / 编辑 / 禁用(启用) / 删除;本人行隐藏禁用/启用/删除(不能操作自己) */
  const selectActions: LmSelectAction<UserData>[] = [
    { title: '详情', onClick: openDetail },
    { title: '编辑', onClick: (row) => openEdit(row as UserData) },
    {
      title: '禁用',
      danger: true,
      show: (row) => row.status === 'active' && !isSelf(row),
      onClick: onToggleStatus,
    },
    {
      title: '启用',
      show: (row) => row.status === 'disabled' && !isSelf(row),
      onClick: onToggleStatus,
    },
    { title: '删除', danger: true, show: (row) => !isSelf(row), onClick: (row) => onRemove(row as UserData) },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, LmSearch, param, search, refresh, hasActiveQuery, reset } = useTable<
    UserData,
    { keyword: string; status: string }
  >({
    request,
    param: { keyword: '', status: '' },
    columns: [
      {
        title: '用户名/邮箱',
        key: 'username',
        minWidth: 180,
        hideable: false,
        render: (row) =>
          h('div', { class: 'flex flex-col' }, [
            h('span', row.username),
            h('span', { class: 'text-xs text-secondary' }, row.email ?? ''),
          ]),
      },
      {
        title: '手机号',
        key: 'phone',
        minWidth: 120,
        render: (row) => row.phone ?? '—',
      },
      {
        title: '角色',
        key: 'role_ids',
        minWidth: 160,
        render: (row) => {
          if (!row.role_ids.length) return '—'
          return h(
            'div',
            { class: 'flex flex-wrap gap-[.4rem]' },
            row.role_ids.map((roleId) =>
              h(
                NTag,
                { key: roleId, size: 'small', bordered: false },
                { default: () => roleNames.value.get(roleId) ?? `#${roleId}` },
              ),
            ),
          )
        },
      },
      {
        title: '状态',
        key: 'status',
        width: 100,
        render: (row) => {
          const conf = userStatusMap[row.status] ?? { label: row.status, type: 'default' }
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
      {
        title: '最后登录',
        key: 'last_login_at',
        width: 150,
        render: (row) => formatDateTime(row.last_login_at),
      },
      {
        title: '创建时间',
        key: 'created_at',
        width: 150,
        render: (row) => formatDateTime(row.created_at),
      },
      useOperate<UserData>(selectActions, { width: 170 }),
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

  /** 状态筛选变更:回第 1 页并请求(后端暂不支持,仅过滤当前页) */
  function onStatusChange(value: string | number) {
    param.status = String(value)
    void search()
  }

  /** 新建用户弹窗 */
  function openCreate() {
    useDialog({
      title: '新建用户',
      component: UserForm,
      area: 45,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑用户弹窗:回填当前行 */
  function openEdit(row: UserData) {
    useDialog({
      title: '编辑用户',
      component: UserForm,
      area: 45,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  // 全局搜索「新建用户」:?action=create 进入时自动打开新建弹窗
  useEntryAction({ create: openCreate })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    statCards,
    statusFilterOptions,
    param,
    onStatusChange,
    hasActiveQuery,
    reset,
    openCreate,
    activeUser,
    detailDrawerShow,
  }
}
