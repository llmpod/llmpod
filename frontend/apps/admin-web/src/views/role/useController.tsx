import { onBeforeUnmount } from 'vue'
import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog, useConfirm, useEntryAction, message } from '@repo/hooks'
import RoleForm from './RoleForm.vue'
import { useStore, roleTypeMap, type RoleData } from './useStore'

export function useController() {
  const { request, remove, subscribe } = useStore()

  /** 当前配置权限的角色 + 权限抽屉显隐 */
  const activeRole = ref<RoleData | null>(null)
  const permissionDrawerShow = ref(false)

  function openPermission(row: RoleData) {
    activeRole.value = row
    permissionDrawerShow.value = true
  }

  /** 删除:二次确认后调用接口;内置角色由后端守卫拒绝 */
  async function onRemove(row: RoleData) {
    try {
      await useConfirm({
        title: '删除角色',
        content: `确定删除角色「${row.name}」吗?删除后不可恢复,已分配该角色的用户将立即失去该角色对应的全部权限。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await remove(row.id)
      message.success(`已删除:${row.name}`)
    } catch {
      // 取消,流程已打断
    }
  }

  /** 行操作:权限(super_admin 除外) / 编辑 / 删除(仅自定义角色) */
  const selectActions: LmSelectAction<RoleData>[] = [
    {
      title: '权限',
      show: (row) => row.code !== 'super_admin',
      onClick: openPermission,
    },
    { title: '编辑', onClick: (row) => openEdit(row as RoleData) },
    {
      title: '删除',
      danger: true,
      show: (row) => !row.is_builtin,
      onClick: (row) => onRemove(row as RoleData),
    },
  ]

  const { LmTable, LmPage, LmRefresh, LmColumn, refresh } = useTable<
    RoleData,
    Record<string, never>
  >({
    request,
    param: {},
    columns: [
      { title: '角色名称', key: 'name', minWidth: 140, hideable: false },
      {
        title: '角色编码',
        key: 'code',
        minWidth: 160,
        // 自定义角色 code 为 null,回退显示角色名称
        render: (row) => row.code ?? row.name,
      },
      {
        title: '类型',
        key: 'is_builtin',
        width: 100,
        render: (row) => {
          const conf = row.is_builtin ? roleTypeMap.builtin : roleTypeMap.custom
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
      useOperate<RoleData>(selectActions, { width: 140 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
    },
  })

  // 订阅 store 变更:新增/编辑/删除/权限保存完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 新建自定义角色弹窗 */
  function openCreate() {
    useDialog({
      title: '新建自定义角色',
      component: RoleForm,
      area: 65,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑角色弹窗:回填当前行(仅支持改名) */
  function openEdit(row: RoleData) {
    useDialog({
      title: '编辑角色',
      component: RoleForm,
      area: 46,
      btn: { confirmText: '保存', cancelText: '取消' },
      props: { initial: row },
      dialogProps: { maskClosable: false },
    })
  }

  // 全局搜索「新建角色」:?action=create 进入时自动打开新建弹窗
  useEntryAction({ create: openCreate })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    openCreate,
    activeRole,
    permissionDrawerShow,
  }
}
