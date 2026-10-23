import { NTag } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate } from '@repo/hooks'
import { listTenants } from '@/api/tenant'
import {
  useStore,
  actionMap,
  actorTypeMap,
  resultMap,
  actionFilterOptions,
  disableFutureDate,
  resourceTypeLabel,
  formatDateTime,
  normalizeDateRange,
  type AuditAction,
  type AuditLogData,
} from './useStore'

export function useController() {
  const { request } = useStore()

  /** 当前查看的日志 + 详情抽屉显隐 */
  const activeLog = ref<AuditLogData | null>(null)
  const detailDrawerShow = ref(false)

  function openDetail(row: AuditLogData) {
    activeLog.value = row
    detailDrawerShow.value = true
  }

  /** 行操作:详情 */
  const selectActions: LmSelectAction<AuditLogData>[] = [{ title: '详情', onClick: openDetail }]

  /** 操作者展示名:用户名快照优先,缺失回退 #id,系统占位 */
  function actorLabel(row: AuditLogData): string {
    if (row.actor_name) return row.actor_name
    return '系统'
  }

  /** 租户 ID → 名称 映射(供表格展示租户名) */
  const tenantMap = ref<Map<number, string>>(new Map())
  /** 租户筛选下拉选项 */
  const tenantFilterOptions = ref<{ label: string; value: number }[]>([])

  /** 拉取租户列表(供租户列展示与筛选下拉);失败不阻塞页面 */
  async function fetchTenants() {
    try {
      const page = await listTenants({ page: 1, page_size: 100 })
      // 平台租户(id=0)不作为筛选维度,「租户」列也统一展示为「平台」,故不入映射
      const tenants = page.items.filter((item) => item.id !== 0)
      tenantMap.value = new Map(tenants.map((item) => [item.id, item.name]))
      tenantFilterOptions.value = tenants.map((item) => ({ label: item.name, value: item.id }))
    } catch {
      // 选项加载失败不阻塞页面,租户列回退 #id 展示
    }
  }

  onMounted(fetchTenants)

  const { LmTable, LmPage, LmRefresh, LmColumn, LmSearch, param, search, hasActiveQuery, reset } =
    useTable<
      AuditLogData,
      {
        action: AuditAction | ''
        tenant_id: number | null
        resource_id: string
        dateRange: [number, number] | null
      }
    >({
      request,
      param: {
        action: '',
        tenant_id: null,
        resource_id: '',
        dateRange: null,
      },
      columns: [
        {
          title: '时间',
          key: 'created_at',
          width: 170,
          hideable: false,
          render: (row) => formatDateTime(row.created_at),
        },
        {
          title: '操作者',
          key: 'actor_name',
          minWidth: 160,
          render: (row) =>
            h('div', { class: 'flex items-center gap-[.5rem]' }, [
              h('span', actorLabel(row)),
              // h(
              //   NTag,
              //   { type: row.actor_type === 'system' ? 'warning' : 'default', size: 'small', bordered: false },
              //   { default: () => actorTypeMap[row.actor_type] ?? row.actor_type },
              // ),
            ]),
        },
        {
          title: '操作',
          key: 'action',
          width: 90,
          render: (row) => {
            const conf = actionMap[row.action] ?? { label: row.action, type: 'default' as const }
            return h(
              NTag,
              { type: conf.type, size: 'small', bordered: false },
              { default: () => conf.label },
            )
          },
        },
        {
          title: '资源',
          key: 'resource_type',
          minWidth: 200,
          render: (row) =>
            h('div', { class: 'flex items-center gap-[.5rem] min-w-0' }, [
              row.resource_type
                ? h(
                    NTag,
                    { type: 'primary', size: 'small', bordered: false },
                    { default: () => resourceTypeLabel(row.resource_type) },
                  )
                : null,
              h('span', { class: 'truncate' }, row.resource_id ?? '--'),
            ]),
        },
        {
          title: '租户',
          key: 'tenant_id',
          width: 140,
          // 平台租户(id=0)与全局日志(tenant_id 为空)统一展示为「平台」;
          // 其余按租户名展示,映射缺失回退 #id
          render: (row) => {
            if (row.tenant_id == null || row.tenant_id === 0) return '平台'
            return tenantMap.value.get(row.tenant_id) ?? `#${row.tenant_id}`
          },
        },
        {
          title: '结果',
          key: 'result',
          width: 90,
          render: (row) => {
            const conf = resultMap[row.result] ?? { label: row.result, type: 'default' as const }
            return h(
              NTag,
              { type: conf.type, size: 'small', bordered: false },
              { default: () => conf.label },
            )
          },
        },
        useOperate<AuditLogData>(selectActions, { width: 80 }),
      ],
      rowKey: 'id',
      config: {
        pagination: { pageSize: 20 },
        tableProps: { flexHeight: false },
      },
    })

  /** 操作筛选变更:回第 1 页并请求 */
  function onActionChange(value: AuditAction | '' | null) {
    param.action = value ?? ''
    void search()
  }

  /** 租户筛选变更:回第 1 页并请求 */
  function onTenantChange(value: number | null) {
    param.tenant_id = value
    void search()
  }

  /** 日期区间变更:归一化为整天范围后回第 1 页并请求 */
  function onDateChange(value: [number, number] | null) {
    param.dateRange = normalizeDateRange(value)
    void search()
  }

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    param,
    actionFilterOptions,
    tenantFilterOptions,
    disableFutureDate,
    onActionChange,
    onTenantChange,
    onDateChange,
    hasActiveQuery,
    reset,
    activeLog,
    detailDrawerShow,
  }
}
