import { NEllipsis, NTag } from 'naive-ui'
import type { SelectOption } from 'naive-ui'
import { LmTableGroup } from '@repo/naive-ui'
import type { LmSelectAction } from '@repo/naive-ui'
import { useTable, useOperate, useDialog } from '@repo/hooks'
import { listTenants, type TenantData } from '@/api/tenant'
import RequestLogDetail from './RequestLogDetail.vue'
import {
  useStore,
  logStatusMap,
  statusFilterOptions,
  upstreamTypeConf,
  formatDecimal,
  formatNumber,
  formatDateTime,
  disableFutureDate,
  type RequestLogData,
} from './useStore'

export function useController() {
  const { request } = useStore()

  /** 租户下拉选项(筛选用;平台 Key 日志 tenant_id 为 null,不属任何租户) */
  const tenantOptions = ref<SelectOption[]>([])

  /** 拉取租户列表填充筛选下拉(失败不阻塞表格,下拉为空) */
  async function fetchTenantOptions() {
    try {
      const tenantPage = await listTenants({ page: 1, page_size: 100 })
      // 排除平台租户(id=0):它不构成筛选维度(平台 Key 日志的 tenant_id 为 null)
      tenantOptions.value = tenantPage.items
        .filter((t: TenantData) => t.id !== 0)
        .map((t: TenantData) => ({
          label: t.name,
          value: t.id,
        }))
    } catch {
      // 选项加载失败不阻塞页面
    }
  }

  onMounted(fetchTenantOptions)

  /** 查看详情:弹窗展示请求/响应 body */
  function openDetail(row: RequestLogData) {
    useDialog({
      title: '调用日志详情',
      component: RequestLogDetail,
      area: 60,
      props: { requestId: row.request_id },
      btn: false,
    })
  }

  const selectActions: LmSelectAction<RequestLogData>[] = [
    { title: '详情', onClick: openDetail },
  ]

  const {
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    param,
    search,
    hasActiveQuery,
    reset,
  } = useTable<
    RequestLogData,
    {
      status: string
      model: string
      api_key_id: number | null
      tenant_id: number | null
      dateRange: [number, number] | null
    }
  >({
    request,
    param: { status: '', model: '', api_key_id: null, tenant_id: null, dateRange: null },
    columns: [
      { title: '模型别名', key: 'model_alias', minWidth: 140, hideable: false },
      { title: '操作', key: 'operation', width: 160 },
      {
        title: '状态',
        key: 'status',
        width: 100,
        render: (row) => {
          const conf = logStatusMap[row.status] ?? { label: row.status, type: 'default' }
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            { default: () => conf.label },
          )
        },
      },
      {
        title: '上游',
        key: 'upstream_type',
        width: 130,
        render: (row) => {
          const conf = upstreamTypeConf(row.upstream_type)
          // 有供应商名时直接以其作为标签文案(不再另起一行输出);
          // 列宽固定,超长由 NEllipsis 按实际宽度省略,仅在溢出时悬浮展示全名
          // (限宽走内联样式:.n-ellipsis 自带 max-width:100%,工具类会因样式表顺序被覆盖)
          return h(
            NTag,
            { type: conf.type, size: 'small', bordered: false },
            {
              default: () =>
                row.provider_name
                  ? h(
                      NEllipsis,
                      { style: { maxWidth: '8rem' } },
                      { default: () => row.provider_name },
                    )
                  : conf.label,
            },
          )
        },
      },
      {
        title: '重试',
        key: 'retry_count',
        width: 80,
        align: 'right',
        render: (row) =>
          row.retry_count > 0
            ? h(
                NTag,
                { type: 'warning', size: 'small', bordered: false },
                { default: () => `${row.retry_count} 次` },
              )
            : h('span', { class: 'text-secondary' }, '0'),
      },
      {
        title: 'Token',
        key: 'total_tokens',
        width: 170,
        align: 'right',
        render: (row) =>
          h('div', { class: 'flex flex-col items-end' }, [
            h('span', `${formatNumber(row.prompt_tokens)} + ${formatNumber(row.completion_tokens)}`),
            h('span', { class: 'text-xs text-secondary' }, `= ${formatNumber(row.total_tokens)}`),
          ]),
      },
      {
        title: '缓存',
        key: 'cached_tokens',
        width: 100,
        align: 'right',
        render: (row) => formatNumber(row.cached_tokens),
      },
      {
        title: '费用',
        key: 'cost',
        width: 110,
        align: 'right',
        render: (row) => formatDecimal(row.cost),
      },
      {
        title: '延迟',
        key: 'latency_ms',
        width: 100,
        align: 'right',
        render: (row) => (row.latency_ms == null ? '—' : `${row.latency_ms} ms`),
      },
      {
        title: '时间',
        key: 'created_at',
        width: 150,
        render: (row) => formatDateTime(row.created_at),
      },
      useOperate<RequestLogData>(selectActions, { width: 90 }),
    ],
    rowKey: 'id',
    config: {
      pagination: { pageSize: 20 },
      tableProps: { flexHeight: false },
      request: { pageSizeField: 'page_size' },
    },
  })

  /** 状态筛选变更:回第 1 页并请求 */
  function onStatusChange(value: string | number) {
    param.status = String(value)
    void search()
  }

  /** 租户筛选变更(清空=全量):回第 1 页并请求 */
  function onTenantIdChange(value: number | null) {
    param.tenant_id = value
    void search()
  }

  /** 时间范围筛选变更(清空=全时间):回第 1 页并请求 */
  function onDateRangeChange(value: [number, number] | null) {
    param.dateRange = value
    void search()
  }

  return {
    LmTableGroup,
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmSearch,
    statusFilterOptions,
    tenantOptions,
    disableFutureDate,
    param,
    onStatusChange,
    onTenantIdChange,
    onDateRangeChange,
    openDetail,
    hasActiveQuery,
    reset,
  }
}
