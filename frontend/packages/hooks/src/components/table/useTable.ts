/* eslint-disable @typescript-eslint/no-explicit-any */
/* eslint-disable vue/one-component-per-file -- useTable 内联短名组件是既定设计 */
import { computed, defineComponent, getCurrentInstance, h, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from 'vue'
import type { DataTableColumn, DataTableInst } from 'naive-ui'
import { useRoute } from 'vue-router'
import {
  LmTable as NaiveLmTable,
  LmTableCard as NaiveLmTableCard,
  LmTablePage as NaiveLmTablePage,
  LmTableRefresh as NaiveLmTableRefresh,
  LmTableColumn as NaiveLmTableColumn,
  LmTableBatch as NaiveLmTableBatch,
  LmTableSearch as NaiveLmTableSearch,
  provideLmTableContext,
  type LmTableContext,
} from '@repo/naive-ui'
import type { TableConfig, TableExtension, UseTableOptions, UseTableReturn } from './table-types'
import { useBreakpoint } from '../../breakpoint'
import { useAutoMaxHeight } from './use-auto-max-height'
import { applyColumnSetting, buildParams, checkActiveQuery, mergeConfig, resolveColumns, sumColumnWidth } from './utils'

/**
 * 组装列偏好持久化 key(纯函数):`llmpod:columns[:<platform>]:<routeName>`。
 * 平台段取 vite base(admin-web=/admin/、tenant-web=/tenant/,双端同 origin 部署靠它隔离);
 * 根路径 '/'(如 portal)无平台概念,省略段;非常规字符的 base 不作平台段使用。
 */
export function buildColumnStorageKey(baseUrl: string, routeName: string): string {
  const segment = baseUrl.replace(/^\//, '').replace(/\/$/, '').trim()
  const platform = /^[a-z0-9][a-z0-9-]*$/i.test(segment) ? segment : ''
  return platform ? `llmpod:columns:${platform}:${routeName}` : `llmpod:columns:${routeName}`
}

/**
 * 自动生成列偏好 storageKey。仅在组件 setup 上下文且路由有名时生成;
 * 否则返回 undefined(保持不持久化)。显式 config.columnSetting.storageKey 永远优先;
 * 同一路由挂多个带列设置的表格时,须显式指定不同 key 避免互相覆盖。
 * import.meta.env 由各 app 的 Vite 注入,cast 以兼容库内 typecheck(同 message/imperative.ts 先例)。
 */
function autoColumnStorageKey(): string | undefined {
  if (!getCurrentInstance()) return undefined
  const name = useRoute()?.name
  if (!name || typeof name !== 'string') return undefined
  const env = (import.meta as unknown as { env?: { BASE_URL?: string } }).env
  return buildColumnStorageKey(env?.BASE_URL || '/', name)
}

/**
 * 表格 Hook:创建表格上下文(请求/分页/选择/列配置/排序筛选)并 provide,
 * 返回 5 个短名组件 + 状态 + 方法。
 *
 * 必须在 <script setup> 顶层调用(provide 需在 setup 同步阶段执行)。
 */
export function useTable<
  Row extends Record<string, any> = Record<string, any>,
  Params extends object = Record<string, any>,
  Other = unknown,
>(options: UseTableOptions<Row, Params, Other>): UseTableReturn<Row, Params, Other> {
  const {
    request,
    columns,
    param: initialParam = {},
    config: userConfig,
    extension = [],
    immediate = true,
    rowKey: rowKeyOption,
    onError,
  } = options

  const config = mergeConfig(userConfig) as TableConfig
  const requestConfig = config.request ?? {}
  const paginationConfig = config.pagination === false ? null : config.pagination

  // === 内置搜索框配置 ===
  const filterConfig = config.filter ?? {}
  const filterMode = filterConfig.mode ?? 'remote'
  const filterFields = filterConfig.fields ?? []
  const filterDebounce = filterConfig.debounce ?? 300
  const fetchPageSize = filterConfig.fetchPageSize

  // === 状态 ===
  const page = ref(paginationConfig?.page ?? 1)
  const pageSize = ref(paginationConfig?.pageSize ?? 20)
  const total = ref(0)
  const data = shallowRef<Row[]>([])
  const loading = ref(false)
  const other = shallowRef<Other | undefined>(undefined)
  const checkedRowKeys = ref<Array<string | number>>([])
  const checkedRows = shallowRef<Row[]>([])
  const sorter = ref<any>(null)
  const filters = ref<any>(null)
  const columnVisibleMap = ref<Record<string, boolean>>({})
  const tableRef = ref<DataTableInst | null>(null)

  const defaultParams = (config.defaultParams ?? {}) as Record<string, any>
  const initialParamMerged = { ...defaultParams, ...(initialParam as object) }
  const param = reactive({ ...initialParamMerged }) as Params

  // === 列 ===
  const rawColumns = computed(() => resolveColumns(columns as DataTableColumn<Row>[]))
  const effectiveColumns = computed(() =>
    applyColumnSetting(rawColumns.value, columnVisibleMap.value),
  )

  // === local 模式:前端过滤 + 本地分页(remote 模式直接用 data/total) ===
  const filterKeyword = ref('')
  /** 是否存在生效的查询条件;空态据此区分"真无数据"与"筛选无结果" */
  const hasActiveQuery = computed(() =>
    checkActiveQuery({
      param: param as Record<string, any>,
      initialParam: initialParamMerged,
      filterKeyword: filterKeyword.value,
      filters: filters.value,
    }),
  )
  const filteredData = computed<Row[]>(() => {
    if (filterMode !== 'local' || !filterFields.length) return data.value
    const keyword = filterKeyword.value.trim().toLowerCase()
    if (!keyword) return data.value
    return data.value.filter((row) =>
      filterFields.some((f) =>
        String((row as any)[f] ?? '')
          .toLowerCase()
          .includes(keyword),
      ),
    )
  })
  const displayData = computed<Row[]>(() => {
    if (filterMode !== 'local') return data.value
    const start = (page.value - 1) * pageSize.value
    return filteredData.value.slice(start, start + pageSize.value)
  })
  const displayTotal = computed(() =>
    filterMode === 'local' ? filteredData.value.length : total.value,
  )

  // local 模式页码收敛:删除/过滤后当前页越界(切片为空且 page>1)时,
  // 自动回到实际最后一页。与 remote 模式「删空回退」同语义,但纯本地切片无需重发请求。
  if (filterMode === 'local') {
    watch(
      () => [filteredData.value.length, pageSize.value] as const,
      () => {
        if (page.value <= 1) return
        const totalPages = Math.max(1, Math.ceil(filteredData.value.length / pageSize.value))
        if (page.value > totalPages) page.value = totalPages
      },
      { flush: 'sync' },
    )
  }

  // === rowKey 归一: keyof Row -> (row) => row[key];naive 层 LmTable 仅接受函数 ===
  const resolvedRowKey = rowKeyOption ?? ('id' as keyof Row)
  const rowKeyFn = (
    typeof resolvedRowKey === 'function'
      ? resolvedRowKey
      : (row: Row) => row[resolvedRowKey] as string | number
  ) as (row: Row) => string | number

  function clearSelection(): void {
    checkedRowKeys.value = []
    checkedRows.value = []
  }

  function setSelection(keys: Array<string | number>): void {
    const keySet = new Set(keys)
    checkedRowKeys.value = keys
    checkedRows.value = data.value.filter((row) => keySet.has(rowKeyFn(row)))
  }

  /** 卡片模式勾选/取消一行:与表格复选列同一状态源(checkedRowKeys/checkedRows) */
  function onCardToggleCheck(row: Row, checked: boolean): void {
    const key = rowKeyFn(row)
    const next = checked
      ? [...checkedRowKeys.value, key]
      : checkedRowKeys.value.filter((k) => k !== key)
    setSelection(next)
  }

  // === 请求(竞态:仅最新请求生效;新请求默认取消上一次未完成请求) ===
  let requestId = 0
  let abortController: AbortController | null = null
  async function refresh(): Promise<void> {
    const id = ++requestId
    if (requestConfig.cancelPrevious !== false) abortController?.abort('由更新的请求替代')
    abortController = new AbortController()
    const signal = abortController.signal
    loading.value = true
    try {
      const params = buildParams({
        defaultParams,
        param,
        page: filterMode === 'local' ? undefined : page.value,
        pageSize: filterMode === 'local' ? fetchPageSize : pageSize.value,
        sorter: sorter.value,
        filters: filters.value,
        pageField: requestConfig.pageField ?? 'page',
        pageSizeField: requestConfig.pageSizeField ?? 'pageSize',
      })
      const result = await request(params, { signal })
      if (id !== requestId) return
      data.value = result.data
      total.value = result.total
      other.value = result.other

      // 当前页删空后自动回退上一页重请求
      if (result.data.length === 0 && page.value > 1) {
        page.value -= 1
        void refresh()
        return
      }

      if (config.selection?.clearAfterRequest) clearSelection()
    } catch (error) {
      if (id !== requestId) return
      onError?.(error)
    } finally {
      if (id === requestId) loading.value = false
    }
  }

  function reset(): Promise<void> {
    Object.keys(param as any).forEach((key) => {
      delete (param as any)[key]
    })
    Object.assign(param as any, initialParamMerged)
    filterKeyword.value = ''
    page.value = 1
    return refresh()
  }

  function search(params?: Partial<Params>): Promise<void> {
    if (params) Object.assign(param as any, params)
    if (requestConfig.resetPageOnSearch !== false) page.value = 1
    return refresh()
  }

  function setParam(params: Partial<Params>): void {
    Object.assign(param as any, params)
  }

  function setData(newData: Row[], newTotal?: number): void {
    data.value = newData
    if (newTotal !== undefined) total.value = newTotal
  }

  /** local 模式更新前端过滤关键字(回到第 1 页,不发请求) */
  function setFilterKeyword(value: string): void {
    filterKeyword.value = value
    page.value = 1
  }

  // === 分页/选择/排序回调 ===
  function onPageChange(p: number): void {
    page.value = p
    // local 模式仅本地切片,不发请求
    if (filterMode !== 'local') void refresh()
  }
  function onPageSizeChange(ps: number): void {
    pageSize.value = ps
    page.value = 1
    if (filterMode !== 'local') void refresh()
  }
  function onCheckedRowKeysChange(keys: Array<string | number>, rows: Row[]): void {
    checkedRowKeys.value = keys
    // naive 跨页保留选择时 rows 可能含 undefined(treeMate 只含当前页),过滤对齐卡片分支语义
    checkedRows.value = rows.filter(Boolean)
  }
  function onUpdateSorter(value: any): void {
    sorter.value = value
  }
  function onUpdateFilters(value: any): void {
    filters.value = value
  }

  // === 列配置 ===
  function setColumnVisible(key: string, visible: boolean): void {
    columnVisibleMap.value = { ...columnVisibleMap.value, [key]: visible }
  }
  function resetColumnSetting(): void {
    columnVisibleMap.value = {}
  }

  // === extension 消费 ===
  const batchActions = extension
    .filter((e): e is Extract<TableExtension, { type: 'batch' }> => e.type === 'batch')
    .flatMap((e) => e.actions)
  extension.forEach((e) => {
    if (e.type === 'refreshList') {
      watch(e.source, (value) => {
        if (value) {
          void refresh()
          e.source.value = false
        }
      })
    }
  })

  // === provide 上下文(供 LmTablePage/Refresh/Column/Batch 经 inject 消费) ===
  const ctx: LmTableContext<Row> = {
    data,
    loading,
    checkedRowKeys,
    checkedRows,
    clearSelection,
    rowKey: rowKeyFn,
    setSelection,
    page,
    pageSize,
    total: displayTotal,
    onPageChange,
    onPageSizeChange,
    refresh,
    rawColumns,
    columnVisibleMap,
    setColumnVisible,
    resetColumnSetting,
    columnStorageKey: config.columnSetting?.storageKey ?? autoColumnStorageKey(),
  }
  provideLmTableContext(ctx)

  // === 5 个短名组件(defineComponent 闭包捕获 state/ctx) ===
  const cardConfig = config.card ?? {}

  const LmTable = defineComponent({
    name: 'UseTableLmTable',
    inheritAttrs: false,
    props: {
      /**
       * 表体最大高度。不传时默认视口自适应:自动测量表格上下方占位后生成
       * calc(100vh - Npx),表格恰好占满剩余可视区且页面不整体滚动;
       * 显式传数字或任意 CSS 高度字符串(向导/弹窗等定高场景)则原样透传。
       */
      maxHeight: { type: [Number, String], default: undefined },
    },
    setup(props, { attrs, slots }) {
      const rootRef = ref<HTMLElement | null>(null)
      const { maxHeight: autoMaxHeight } = useAutoMaxHeight(rootRef)
      const { isMobile } = useBreakpoint()
      return () => {
        // 窄屏卡片形态(≤767px):列定义转 label-value 卡片,数据/分页/选择复用上下文;
        // 卡片自然撑高走页面滚动,不消费 maxHeight;empty 插槽透传
        if (isMobile.value && cardConfig.enabled !== false) {
          return h(
            'div',
            { ref: rootRef, ...attrs },
            h(
              NaiveLmTableCard,
              {
                rows: displayData.value,
                columns: effectiveColumns.value,
                rowKey: rowKeyFn,
                checkedRowKeys: checkedRowKeys.value,
                loading: loading.value,
                titleKey: cardConfig.titleKey ?? '',
                fieldKeys: cardConfig.fieldKeys,
                emphasisKey: cardConfig.emphasisKey ?? '',
                tagKey: cardConfig.tagKey ?? '',
                alertWhen: cardConfig.alertWhen,
                density: cardConfig.density ?? 'card',
                onToggleCheck: onCardToggleCheck,
              } as any,
              { empty: slots.empty },
            ),
          )
        }
        const tableProps = (config.tableProps ?? {}) as Record<string, unknown>
        // 优先级:模板显式传入 > config.tableProps > 视口自适应默认
        const maxHeight = props.maxHeight ?? tableProps.maxHeight ?? autoMaxHeight.value
        // 默认 scrollX:声明列宽总和作内容最小宽。naive 在 maxHeight/flexHeight 场景下
        // 不传 scrollX 则 xScrollable 恒 false(横向 rail 不渲染),窄屏溢出无从滚动;
        // 全列声明宽才估算,弹性列/分组列保持原状。显式值(模板/config)优先。
        const scrollX =
          (attrs.scrollX ?? attrs['scroll-x'] ?? tableProps.scrollX) ??
          sumColumnWidth(effectiveColumns.value)
        return h(
          'div',
          { ref: rootRef },
          h(
            NaiveLmTable,
            {
              ...tableProps,
              ...attrs,
              ...(maxHeight !== undefined ? { maxHeight } : {}),
              ...(scrollX !== undefined ? { scrollX } : {}),
              ref: tableRef,
              data: displayData.value,
              loading: loading.value,
              columns: effectiveColumns.value,
              rowKey: rowKeyFn,
              checkedRowKeys: checkedRowKeys.value,
              'onUpdate:checkedRowKeys': onCheckedRowKeysChange,
              'onUpdate:sorter': onUpdateSorter,
              'onUpdate:filters': onUpdateFilters,
            } as any,
            slots,
          ),
        )
      }
    },
  })

  const LmPage = defineComponent({
    name: 'UseTableLmPage',
    inheritAttrs: false,
    setup(_, { attrs, slots }) {
      return () => h(NaiveLmTablePage, attrs, slots)
    },
  })

  const LmRefresh = defineComponent({
    name: 'UseTableLmRefresh',
    inheritAttrs: false,
    setup(_, { attrs, slots }) {
      return () => h(NaiveLmTableRefresh, attrs, slots)
    },
  })

  const LmColumn = defineComponent({
    name: 'UseTableLmColumn',
    inheritAttrs: false,
    setup(_, { attrs, slots }) {
      return () => h(NaiveLmTableColumn, attrs, slots)
    },
  })

  const LmBatch = defineComponent({
    name: 'UseTableLmBatch',
    inheritAttrs: false,
    setup(_, { attrs, slots }) {
      return () => h(NaiveLmTableBatch, { actions: batchActions, ...attrs }, slots)
    },
  })

  const LmSearch = defineComponent({
    name: 'UseTableLmSearch',
    inheritAttrs: false,
    props: {
      field: { type: String, default: 'keyword' },
    },
    setup(props, { attrs, slots }) {
      return () => {
        const { debounce: attrsDebounce, ...rest } = attrs as any
        return h(
          NaiveLmTableSearch,
          {
            ...rest,
            debounce: attrsDebounce ?? filterDebounce,
            modelValue:
              filterMode === 'local'
                ? filterKeyword.value
                : String((param as any)[props.field] ?? ''),
            onSearch: (value: string) => {
              if (filterMode === 'local') {
                setFilterKeyword(value)
              } else {
                void search({ [props.field]: value } as Partial<Params>)
              }
            },
          } as any,
          slots,
        )
      }
    },
  })

  if (immediate) {
    onMounted(() => {
      void refresh()
    })
  }

  // 组件卸载后作废 requestId 并取消在途请求,避免回调仍写入 data/loading(文档要求"卸载后取消请求或忽略响应",F-009)。
  // requestId 递增在前:即便 abort 让 fetch reject,catch 内 `id !== requestId` 也会提前返回,不触发 onError
  onUnmounted(() => {
    requestId += 1
    abortController?.abort('unmounted')
  })

  return {
    LmTable,
    LmPage,
    LmRefresh,
    LmColumn,
    LmBatch,
    LmSearch,
    tableRef,
    param,
    config,
    data,
    total,
    loading,
    hasActiveQuery,
    other,
    selectedKeys: checkedRowKeys,
    selectedRows: checkedRows,
    refresh,
    reset,
    search,
    setParam,
    setData,
    clearSelection,
  }
}
