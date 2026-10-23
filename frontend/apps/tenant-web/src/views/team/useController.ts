import { computed, h, onBeforeUnmount, ref, watch } from 'vue'
import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { message, useConfirm, useDialog, useEntryAction, useOperate, useTable } from '@repo/hooks'
import MemberDetailDrawer from './MemberDetailDrawer.vue'
import MemberForm from './MemberForm.vue'
import {
  useStore,
  memberStatusMap,
  statusFilterOptions,
  formatDateTime,
  type Member,
} from './useStore'

export function useController() {
  const { records, request, roleName, toggleStatus, removeMember, subscribe } = useStore()

  /** 状态筛选(空值表示全部成员) */
  const status = ref('')

  /** 详情抽屉:记录成员 id,内容从 records 实时取,保证禁用/启用后同步 */
  const detailShow = ref(false)
  const detailMemberId = ref<number | null>(null)
  const detailMember = computed(
    () => records.value.find((r) => r.id === detailMemberId.value) ?? null,
  )

  /** 顶部统计卡片:基于全量成员统计 */
  const statCards = computed<{ label: string; value: string; tone?: 'error' }[]>(() => [
    { label: '总成员', value: String(records.value.length) },
    { label: '启用', value: String(records.value.filter((r) => r.status === 'active').length) },
    {
      label: '已禁用',
      value: String(records.value.filter((r) => r.status === 'disabled').length),
      tone: 'error' as const,
    },
  ])

  /** 行操作:详情 / 编辑 / 禁用启用(按当前状态二选一) / 移除 */
  const selectActions: LmSelectAction<Member>[] = [
    { title: '详情', onClick: openDetail },
    { title: '编辑', onClick: openEdit },
    {
      title: '禁用',
      danger: true,
      show: (row) => row.status === 'active',
      onClick: handleToggleStatus,
    },
    {
      title: '启用',
      show: (row) => row.status !== 'active',
      onClick: handleToggleStatus,
    },
    { title: '移除', danger: true, onClick: handleRemove },
  ]

  const { LmTable, LmPage, LmSearch, refresh, setParam, hasActiveQuery, reset } = useTable<Member>({
    request,
    param: { status: '' },
    columns: [
      {
        title: '用户名/邮箱',
        key: 'username',
        minWidth: 180,
        hideable: false,
        render: (row) =>
          h('div', { class: 'flex flex-col' }, [
            h('span', { class: 'text-strong' }, row.username),
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
        minWidth: 140,
        render: (row) =>
          row.role_ids.length
            ? h(
                'div',
                { class: 'flex flex-wrap gap-[.5rem]' },
                row.role_ids.map((id) =>
                  h(
                    NTag,
                    { key: id, size: 'small', type: 'primary', bordered: false },
                    { default: () => roleName(id) },
                  ),
                ),
              )
            : '—',
      },
      {
        title: '状态',
        key: 'status',
        minWidth: 100,
        render: (row) => {
          const conf = memberStatusMap[row.status] ?? { label: row.status, type: 'default' }
          return h(
            NTag,
            { size: 'small', type: conf.type, bordered: false },
            { default: () => conf.label },
          )
        },
      },
      {
        title: '最后登录',
        key: 'last_login_at',
        minWidth: 150,
        render: (row) => formatDateTime(row.last_login_at),
      },
      {
        title: '创建时间',
        key: 'created_at',
        minWidth: 150,
        render: (row) => formatDateTime(row.created_at),
      },
      useOperate<Member>(selectActions, { width: 150 }),
    ],
    rowKey: 'id',
    config: {
      // local 模式:一次性取全量,用户名/邮箱搜索与分页在前端完成
      filter: { mode: 'local', fields: ['username', 'email'] },
      tableProps: { flexHeight: false },
    },
  })

  // 状态筛选变化:下发查询参数并重新取全量数据(接口无过滤参数,store 内前端过滤)
  watch(status, (value) => {
    setParam({ status: value })
    void refresh()
  })

  // 订阅 store 变更:禁用/启用/移除完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 清空筛选:同步本地筛选 ref 后重置表格查询 */
  function resetFilters() {
    status.value = ''
    void reset()
  }

  /** 打开成员详情抽屉 */
  function openDetail(row: Member) {
    detailMemberId.value = row.id
    detailShow.value = true
  }

  /** 创建成员弹窗 */
  function openCreate() {
    useDialog({
      title: '创建成员',
      component: MemberForm,
      area: 44,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑成员弹窗:回填当前行(username 不可改,密码留空不变,角色走角色管理页) */
  function openEdit(row: Member) {
    useDialog({
      title: '编辑成员',
      component: MemberForm,
      area: 44,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  /** 禁用/启用:二次确认后切换状态 */
  async function handleToggleStatus(row: Member) {
    const disabling = row.status === 'active'
    try {
      await useConfirm({
        title: disabling ? '确认禁用' : '确认启用',
        content: disabling
          ? `禁用后「${row.username}」将无法登录和调用 API,确定禁用吗?`
          : `确定恢复「${row.username}」的使用吗?`,
        confirmText: disabling ? '禁用' : '启用',
        cancelText: '取消',
      })
      const next = await toggleStatus(row)
      message.success(next === 'disabled' ? `已禁用:${row.username}` : `已启用:${row.username}`)
    } catch {
      // 取消,流程已打断
    }
  }

  /** 移除成员:二次确认后删除 */
  async function handleRemove(row: Member) {
    try {
      await useConfirm({
        title: '确认移除',
        content: `确定将「${row.username}」移出团队吗?移除后不可恢复。`,
        confirmText: '移除',
        cancelText: '取消',
      })
      await removeMember(row.id)
      message.success(`已移除:${row.username}`)
    } catch {
      // 取消,流程已打断
    }
  }

  // 全局搜索「创建成员」:?action=create 进入时自动打开新建弹窗
  useEntryAction({ create: openCreate })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmSearch,
    MemberDetailDrawer,
    status,
    statusFilterOptions,
    hasActiveQuery,
    resetFilters,
    statCards,
    detailShow,
    detailMember,
    openCreate,
  }
}
