import { onBeforeUnmount, ref, onMounted } from 'vue'
import { NTag, NButton, NIcon, NSpace } from 'naive-ui'
import { CopyOutline, CheckmarkOutline } from '@vicons/ionicons5'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import type { SelectOption } from 'naive-ui'
import { useTable, useOperate, useDialog, useConfirm, useEntryAction, message, copyToClipboard } from '@repo/hooks'
import { listTenants, type TenantData } from '@/api/tenant'
import { listPublishes } from '@/api/publish'
import ApiKeyForm from './ApiKeyForm.vue'
import {
  useStore,
  apiKeyStatusMap,
  statusFilterOptions,
  formatDecimal,
  formatDateTime,
  type ApiKeyData,
} from './useStore'

export function useController() {
  const { request, disable, enable, remove, resetBudget, subscribe } = useStore()

  /** 租户 ID → 名称 映射（供表格展示租户名） */
  const tenantMap = ref<Map<number, string>>(new Map())
  /** 租户下拉选项（新建表单用） */
  const tenantOptions = ref<SelectOption[]>([])
  /** 已发布模型别名候选（新建表单授权模型 tag 用） */
  const modelAliases = ref<string[]>([])

  /** 复制成功后短暂显示对勾的记录 id */
  const copiedId = ref<number | string>('')
  let copyTimer: ReturnType<typeof setTimeout> | undefined

  /** 拉取租户列表与已发布模型别名（供表格展示与表单选项） */
  async function fetchOptions() {
    try {
      const [tenantPage, publishes] = await Promise.all([
        listTenants({ page: 1, page_size: 100 }),
        listPublishes(),
      ])
      tenantMap.value = new Map(tenantPage.items.map((t: TenantData) => [t.id, t.name]))
      tenantOptions.value = tenantPage.items.map((t: TenantData) => ({
        label: t.name,
        value: t.id,
      }))
      modelAliases.value = publishes.map((p) => p.route.name)
    } catch {
      // 选项加载失败不阻塞页面，表单选项为空
    }
  }

  onMounted(fetchOptions)

  /** Key 明文/哈希等长串中间省略展示：保留前 8 位与后 4 位 */
  function maskSecret(secret: string) {
    if (secret.length <= 16) return secret
    return `${secret.slice(0, 8)}...${secret.slice(-4)}`
  }

  /** 复制 Key 明文到剪贴板 */
  async function copyPlaintext(row: ApiKeyData) {
    try {
      await copyToClipboard(row.plaintext)
      copiedId.value = row.id
      clearTimeout(copyTimer)
      copyTimer = setTimeout(() => {
        copiedId.value = ''
      }, 1500)
    } catch {
      message.error('复制失败，请手动复制')
    }
  }

  /** 禁用：二次确认（记录保留，可重新启用） */
  async function onDisable(row: ApiKeyData) {
    try {
      await useConfirm({
        title: '禁用 API Key',
        content: `确定禁用「${row.name}」吗？禁用后该 Key 立即失效，可重新启用。`,
        confirmText: '禁用',
        cancelText: '取消',
      })
      await disable(row.id)
      message.success(`已禁用：${row.name}`)
    } catch {
      // 取消，流程已打断
    }
  }

  /** 启用：二次确认后立即恢复可用 */
  async function onEnable(row: ApiKeyData) {
    try {
      await useConfirm({
        title: '启用 API Key',
        content: `确定启用「${row.name}」吗？启用后该 Key 立即恢复可用。`,
        confirmText: '启用',
        cancelText: '取消',
      })
      await enable(row.id)
      message.success(`已启用：${row.name}`)
    } catch {
      // 取消，流程已打断
    }
  }

  /** 删除：二次确认（彻底删除，不可恢复） */
  async function onRemove(row: ApiKeyData) {
    try {
      await useConfirm({
        title: '删除 API Key',
        content: `确定删除「${row.name}」吗？删除后彻底移除，不可恢复。`,
        confirmText: '删除',
        cancelText: '取消',
      })
      await remove(row.id)
      message.success(`已删除：${row.name}`)
    } catch {
      // 取消，流程已打断
    }
  }

  /** 重置预算累计：二次确认 */
  async function onResetBudget(row: ApiKeyData) {
    try {
      await useConfirm({
        title: '重置预算累计',
        content: `确定重置「${row.name}」的已消费累计吗？budget_spent 将清零（budget_frozen 不受影响）。`,
        confirmText: '重置',
        cancelText: '取消',
      })
      await resetBudget(row.id)
      message.success(`已重置预算：${row.name}`)
    } catch {
      // 取消
    }
  }

  /** 行操作：调整 / 重置预算 / 禁用(启用) / 删除 */
  const selectActions: LmSelectAction<ApiKeyData>[] = [
    { title: '调整', onClick: (row) => openEdit(row as ApiKeyData) },
    {
      title: '重置预算',
      show: (row) => row.status === 'active',
      onClick: (row) => onResetBudget(row as ApiKeyData),
    },
    {
      title: '禁用',
      danger: true,
      show: (row) => row.status === 'active',
      onClick: (row) => onDisable(row as ApiKeyData),
    },
    {
      title: '启用',
      show: (row) => row.status === 'disabled',
      onClick: (row) => onEnable(row as ApiKeyData),
    },
    {
      title: '删除',
      danger: true,
      onClick: (row) => onRemove(row as ApiKeyData),
    },
  ]

  const {
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    param,
    search,
    refresh,
    hasActiveQuery,
    reset,
  } = useTable<ApiKeyData, { keyword: string; status: string }>({
    request,
    param: { keyword: '', status: '' },
    columns: [
      { title: '名称', key: 'name', minWidth: 140, hideable: false },
      {
        title: 'API Key',
        key: 'plaintext',
        minWidth: 200,
        render: (row) =>
          row.plaintext
            ? h('div', { class: 'flex items-center gap-[.25rem]' }, [
                h('span', { class: 'font-mono text-xs', title: row.plaintext }, maskSecret(row.plaintext)),
                h(
                  NButton,
                  {
                    text: true,
                    size: 'tiny',
                    type: copiedId.value === row.id ? 'primary' : 'default',
                    title: '复制',
                    onClick: () => copyPlaintext(row),
                  },
                  {
                    icon: () =>
                      h(NIcon, {
                        component: copiedId.value === row.id ? CheckmarkOutline : CopyOutline,
                      }),
                    default: () => (copiedId.value === row.id ? '已复制' : ''),
                  },
                ),
              ])
            : '—',
      },
      {
        title: '所属租户',
        key: 'tenant_id',
        width: 120,
        render: (row) =>
          row.tenant_id === null ? '平台直发' : (tenantMap.value.get(row.tenant_id) ?? `#${row.tenant_id}`),
      },
      {
        title: '授权模型',
        key: 'allowed_models',
        minWidth: 200,
        render: (row) =>
          row.allowed_models.length
            ? h(
                'div',
                { class: 'flex flex-wrap gap-[.25rem]' },
                row.allowed_models.map((model) =>
                  h(
                    NTag,
                    { type: 'primary', size: 'small', bordered: false },
                    { default: () => model },
                  ),
                ),
              )
            : h(NTag, { size: 'small', bordered: false }, { default: () => '全部模型' }),
      },
      {
        title: '预算',
        key: 'budget',
        minWidth: 160,
        render: (row) =>
          h('div', { class: 'flex flex-col text-xs' }, [
            h('span', null, `已消费 ${formatDecimal(row.budget_spent)}`),
            row.budget_limit
              ? h('span', { class: 'text-secondary' }, `上限 ${formatDecimal(row.budget_limit)}`)
              : null,
          ]),
      },
      {
        title: '状态',
        key: 'status',
        width: 100,
        render: (row) => {
          const conf = apiKeyStatusMap[row.status] ?? { label: row.status, type: 'default' }
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
      {
        title: '过期时间',
        key: 'expires_at',
        width: 150,
        render: (row) => (row.expires_at ? formatDateTime(row.expires_at) : '永不过期'),
      },
      useOperate<ApiKeyData>(selectActions, { width: 150 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 10 },
      tableProps: { flexHeight: false },
      filter: {
        mode: 'local',
        fields: ['name', 'plaintext'],
      },
    },
  })

  // 订阅 store 变更：新建/禁用/启用/删除/调整/重置预算完成后自动刷新表格
  const unsubscribe = subscribe(() => {
    void refresh()
  })
  onBeforeUnmount(unsubscribe)

  /** 状态筛选变更：local 模式下前端过滤 */
  function onStatusChange(value: string | number) {
    param.status = String(value)
    void search()
  }

  /** 新建 API Key 弹窗 */
  function openCreate() {
    useDialog({
      title: '新建 API Key',
      component: ApiKeyForm,
      props: {
        tenants: tenantOptions.value,
        modelAliases: modelAliases.value,
      },
      area: 48,
      btn: { confirmText: '创建', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  /** 编辑 API Key 弹窗：回填当前行 */
  function openEdit(row: ApiKeyData) {
    useDialog({
      title: '调整 API Key',
      component: ApiKeyForm,
      props: {
        initial: row,
        tenants: tenantOptions.value,
        modelAliases: modelAliases.value,
      },
      area: 48,
      btn: { confirmText: '保存', cancelText: '取消' },
      dialogProps: { maskClosable: false },
    })
  }

  // 全局搜索「新建 API Key」：?action=create 进入时自动打开新建弹窗
  useEntryAction({ create: openCreate })

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    param,
    statusFilterOptions,
    onStatusChange,
    hasActiveQuery,
    reset,
    openCreate,
  }
}
