// @vitest-environment happy-dom
import { h, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { LmTable as NaiveLmTable, LmTableSearch as NaiveLmTableSearch } from '@repo/naive-ui'
import { useTable } from '../src/components/table/useTable'
import { applyColumnSetting, buildParams, mergeConfig } from '../src/components/table/utils'
import type { DataTableColumn } from 'naive-ui'

// ---------- utils 纯函数 ----------

describe('mergeConfig 配置合并', () => {
  it('无用户配置返回默认值拷贝(两次调用互不影响)', () => {
    const first = mergeConfig()
    const second = mergeConfig()
    expect(first.pagination).toMatchObject({ page: 1, pageSize: 20 })
    expect(first).not.toBe(second)
  })

  it('用户配置覆盖顶层与嵌套字段,pagination:false 关闭分页', () => {
    const config = mergeConfig({ pagination: { pageSize: 50 }, request: { pageField: 'page_no' } })
    expect(config.pagination).toMatchObject({ page: 1, pageSize: 50, showSizePicker: true })
    expect(config.request?.pageField).toBe('page_no')
    expect(config.request?.pageSizeField).toBe('pageSize') // 未覆盖字段保留默认

    expect(mergeConfig({ pagination: false }).pagination).toBe(false)
  })

  it('tableProps 逐键合并:部分覆盖不丢 striped/remote 等默认(NT-01)', () => {
    const config = mergeConfig({ tableProps: { flexHeight: false } })
    expect(config.tableProps).toMatchObject({ striped: true, remote: true, flexHeight: false })
  })

  it('tableProps 与模块级默认断链:改 merged 不污染后续调用(NT-01)', () => {
    const config = mergeConfig({ tableProps: { flexHeight: false } })
    config.tableProps.remote = false
    config.tableProps.newKey = 'x'
    const fresh = mergeConfig({ tableProps: { flexHeight: false } })
    expect(fresh.tableProps.remote).toBe(true)
    expect(fresh.tableProps).not.toHaveProperty('newKey')
  })
})

describe('applyColumnSetting 列显隐', () => {
  const columns = [
    { type: 'selection' },
    { title: '名称', key: 'name', hideable: false },
    { title: '备注', key: 'remark' },
    { title: '操作', key: 'actions', __lmOperate: true },
    { title: '无key列' },
  ] as unknown as DataTableColumn[]

  it('被隐藏的可隐藏列被过滤,selection/操作列/无key列始终保留', () => {
    const result = applyColumnSetting(columns, { remark: false })
    expect(result.map((col) => (col as { key?: string }).key)).toEqual([
      undefined,
      'name',
      'actions',
      undefined,
    ])
  })

  it('锁定列(hideable:false)即使 visibleMap 中为 false 也始终保留', () => {
    const result = applyColumnSetting(columns, { name: false, remark: false })
    expect(result.map((col) => (col as { key?: string }).key)).toContain('name')
    expect(result.map((col) => (col as { key?: string }).key)).not.toContain('remark')
  })

  it('空 visibleMap 全量保留(resetColumnSetting 语义即清空 map)', () => {
    expect(applyColumnSetting(columns, {})).toHaveLength(5)
  })
})

describe('buildParams 请求参数拼装', () => {
  it('defaultParams 被 param 覆盖,page/pageSize 用自定义字段名', () => {
    const params = buildParams({
      defaultParams: { status: 'a', extra: 1 },
      param: { status: 'b' },
      page: 2,
      pageSize: 30,
      sorter: null,
      filters: null,
      pageField: 'page_no',
      pageSizeField: 'page_size',
    })
    expect(params).toMatchObject({ status: 'b', extra: 1, page_no: 2, page_size: 30 })
  })

  it('page/pageSize 为 undefined 时不附加分页字段(local 全量场景)', () => {
    const params = buildParams({
      param: { keyword: 'x' },
      page: undefined,
      pageSize: undefined,
      sorter: null,
      filters: null,
      pageField: 'page',
      pageSizeField: 'pageSize',
    })
    expect(params).not.toHaveProperty('page')
    expect(params).not.toHaveProperty('pageSize')
    expect(params).toMatchObject({ keyword: 'x', sorter: null, filters: null })
  })
})

// ---------- useTable 行为(在组件 setup 内创建) ----------

interface Row {
  id: number
  name: string
  status: string
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

type TableApi = ReturnType<typeof useTable<Row, Record<string, unknown>>>

/** 在真实组件 setup 内创建 useTable(provide/onMounted 需要 setup 上下文) */
function setupTable(options: Parameters<typeof useTable>[0]) {
  let api!: TableApi
  const wrapper = mount(
    {
      setup() {
        api = useTable(options) as TableApi
        return () => h('div')
      },
    },
    { attachTo: document.body },
  )
  return { api, wrapper }
}

/** 在 setup 内创建并渲染 LmSearch(驱动 onSearch 用) */
function setupTableWithSearch(options: Parameters<typeof useTable>[0]) {
  let api!: TableApi
  const wrapper = mount(
    {
      setup() {
        api = useTable(options) as TableApi
        return () => h('div', [h(api.LmSearch, { field: 'keyword' })])
      },
    },
    { attachTo: document.body },
  )
  return { api, wrapper }
}

async function flush() {
  await Promise.resolve()
  await nextTick()
  await Promise.resolve()
}

describe('useTable 请求生命周期', () => {
  it('immediate 挂载即请求,参数含分页字段与 signal,写入 data/total/other', async () => {
    const request = vi
      .fn()
      .mockResolvedValue({ data: [{ id: 1, name: 'a', status: 'ok' }], total: 1, other: { summary: 'x' } })
    const { api, wrapper } = setupTable({ request, columns: [] })
    await vi.waitFor(() => expect(api.loading.value).toBe(false))

    expect(request).toHaveBeenCalledTimes(1)
    const [params, ctx] = request.mock.calls[0]
    expect(params).toMatchObject({ page: 1, pageSize: 20, sorter: null, filters: null })
    expect(ctx.signal).toBeInstanceOf(AbortSignal)
    expect(api.data.value).toHaveLength(1)
    expect(api.total.value).toBe(1)
    expect(api.other.value).toEqual({ summary: 'x' })
    wrapper.unmount()
  })

  it('immediate:false 挂载不请求,手动 refresh 才触发', async () => {
    const request = vi.fn().mockResolvedValue({ data: [], total: 0 })
    const { api, wrapper } = setupTable({ request, columns: [], immediate: false })
    await flush()
    expect(request).not.toHaveBeenCalled()
    await api.refresh()
    expect(request).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('竞态守卫:慢的旧响应被丢弃,不写 data/loading', async () => {
    const slow = deferred<{ data: Row[]; total: number }>()
    const request = vi
      .fn()
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce({ data: [{ id: 2, name: 'b', status: 'ok' }], total: 1 })
    const { api, wrapper } = setupTable({ request, columns: [], immediate: false })

    const first = api.refresh()
    const second = api.refresh()
    await second
    expect(api.data.value[0].id).toBe(2)

    slow.resolve({ data: [{ id: 99, name: 'stale', status: 'x' }], total: 99 })
    await first
    await flush()
    expect(api.data.value[0].id).toBe(2) // 旧响应未覆盖
    expect(api.loading.value).toBe(false)
    wrapper.unmount()
  })

  it('cancelPrevious:新请求中止上一次在途 signal', async () => {
    const pending = deferred<{ data: Row[]; total: number }>()
    const request = vi
      .fn()
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce({ data: [], total: 0 })
    const { api, wrapper } = setupTable({ request, columns: [], immediate: false })

    void api.refresh()
    const firstSignal = request.mock.calls[0][1].signal
    expect(firstSignal.aborted).toBe(false)

    void api.refresh()
    expect(firstSignal.aborted).toBe(true)
    pending.resolve({ data: [], total: 0 })
    wrapper.unmount()
  })

  it('当前页删空且 page>1 时自动回退上一页并重请求', async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce({ data: [], total: 20 }) // 初始即第2页且空
      .mockResolvedValueOnce({ data: [{ id: 5, name: 'e', status: 'ok' }], total: 20 })
    const { api, wrapper } = setupTable({
      request,
      columns: [],
      immediate: false,
      config: { pagination: { page: 2 } },
    })

    await api.refresh()
    await flush()
    expect(request).toHaveBeenCalledTimes(2) // 回退第1页自动重请求
    expect(request.mock.calls[1][0]).toMatchObject({ page: 1 })
    expect(api.data.value[0].id).toBe(5)
    wrapper.unmount()
  })

  it('复现用户场景:第2页仅1条,删除后刷新返回空页,应回退第1页并展示数据', async () => {
    const page1: Row[] = Array.from({ length: 10 }, (_, i) => ({ id: i + 1, name: `a${i}`, status: 'ok' }))
    const request = vi
      .fn()
      .mockResolvedValueOnce({ data: [{ id: 11, name: 'last', status: 'ok' }], total: 11 }) // 第2页仅1条
      .mockResolvedValueOnce({ data: [], total: 10 }) // 删除后第2页已空
      .mockResolvedValueOnce({ data: page1, total: 10 }) // 回退后第1页
    const { api, wrapper } = setupTable({
      request,
      columns: [],
      immediate: false,
      config: { pagination: { page: 2, pageSize: 10 } },
    })

    await api.refresh() // 初始:第2页1条
    expect(api.data.value).toHaveLength(1)

    await api.refresh() // 模拟删除后的 subscribe → refresh
    await flush()
    expect(request).toHaveBeenCalledTimes(3)
    expect(request.mock.calls[2][0]).toMatchObject({ page: 1 }) // 回退后重请求第1页
    expect(api.data.value).toHaveLength(10)
    wrapper.unmount()
  })

  it('请求失败触发 onError 且被吞掉;被竞态作废的失败不触发', async () => {
    const onError = vi.fn()
    const stale = deferred<{ data: Row[]; total: number }>()
    const request = vi
      .fn()
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce({ data: [], total: 0 })
      .mockRejectedValueOnce(new Error('boom'))
    const { api, wrapper } = setupTable({ request, columns: [], immediate: false, onError })

    // 旧请求失败但已被新请求作废 → 不触发 onError
    void api.refresh()
    void api.refresh()
    stale.reject(new Error('stale-fail'))
    await flush()
    expect(onError).not.toHaveBeenCalled()

    // 最新请求失败 → 触发 onError,refresh 不 reject
    await expect(api.refresh()).resolves.toBeUndefined()
    expect(onError).toHaveBeenCalledTimes(1)
    expect(api.loading.value).toBe(false)
    wrapper.unmount()
  })

  it('卸载后作废在途请求:signal 中止,迟到响应不写 data', async () => {
    const pending = deferred<{ data: Row[]; total: number }>()
    const request = vi.fn().mockReturnValue(pending.promise)
    const { api, wrapper } = setupTable({ request, columns: [], immediate: false })

    void api.refresh()
    const signal = request.mock.calls[0][1].signal
    wrapper.unmount()
    expect(signal.aborted).toBe(true)

    pending.resolve({ data: [{ id: 9, name: 'late', status: 'x' }], total: 9 })
    await flush()
    expect(api.data.value).toHaveLength(0)
  })
})

describe('useTable 查询与参数', () => {
  it('search 合并参数并重置页码为1', async () => {
    const request = vi.fn().mockResolvedValue({ data: [], total: 0 })
    const { api, wrapper } = setupTable({ request, columns: [], immediate: false })
    await api.search({ keyword: 'k', status: 'active' })

    expect(request.mock.calls[0][0]).toMatchObject({ keyword: 'k', status: 'active', page: 1 })
    wrapper.unmount()
  })

  it('reset 恢复初始参数(defaultParams+param)并回第1页', async () => {
    const request = vi.fn().mockResolvedValue({ data: [], total: 0 })
    const { api, wrapper } = setupTable({
      request,
      columns: [],
      immediate: false,
      param: { status: 'init' },
      config: { defaultParams: { env: 'test' } },
    })

    await api.search({ status: 'changed', extra: 'e' })
    expect(request.mock.calls[0][0]).toMatchObject({ status: 'changed', extra: 'e', env: 'test' })

    await api.reset()
    expect(request.mock.calls[1][0]).toMatchObject({ status: 'init', env: 'test', page: 1 })
    expect(request.mock.calls[1][0].extra).toBeUndefined()
    wrapper.unmount()
  })

  it('setParam 只改参数不发请求,setData 直接写数据', async () => {
    const request = vi.fn().mockResolvedValue({ data: [], total: 0 })
    const { api, wrapper } = setupTable({ request, columns: [], immediate: false })

    api.setParam({ keyword: 'silent' })
    await flush()
    expect(request).not.toHaveBeenCalled()

    api.setData([{ id: 1, name: 'local', status: 'ok' }], 1)
    expect(api.data.value).toHaveLength(1)
    expect(api.total.value).toBe(1)
    wrapper.unmount()
  })

  it('local 模式:删除尾页最后一条后自动回退页码,不停留空页', async () => {
    let rows: Row[] = Array.from({ length: 11 }, (_, i) => ({ id: i + 1, name: `a${i}`, status: 'ok' }))
    const request = vi.fn(async () => ({ data: [...rows], total: rows.length }))
    let api!: TableApi
    const wrapper = mount(
      {
        setup() {
          api = useTable({
            request,
            columns: [],
            immediate: false,
            config: {
              pagination: { page: 1, pageSize: 10 },
              filter: { mode: 'local', fields: ['name'] },
            },
          }) as TableApi
          return () => h('div', [h(api.LmTable), h(api.LmPage)])
        },
      },
      { attachTo: document.body },
    )

    await api.refresh() // 全量 11 条
    // naive-ui NPagination 组件 name 为 'Pagination'
    const pagination = () => wrapper.findComponent({ name: 'Pagination' })
    const table = () => wrapper.findComponent(NaiveLmTable)

    // 翻到第 2 页(local 纯本地切片,不发请求),此时第 2 页仅 1 条
    pagination().vm.$emit('update:page', 2)
    await flush()
    expect(table().props('data')).toHaveLength(1)

    // 模拟删除第 2 页唯一一条后 store notify → refresh(全量请求,返回非空前 N 条)
    rows = rows.slice(0, 10)
    await api.refresh()
    await flush()

    // 自动回退第 1 页:表格收到第 1 页 10 条,不再停留空页
    expect(table().props('data')).toHaveLength(10)
    wrapper.unmount()
  })

  it('排序/筛选回调写入状态并随下次请求透传', async () => {
    const request = vi.fn().mockResolvedValue({ data: [], total: 0 })
    let api!: TableApi
    const wrapper = mount(
      {
        setup() {
          api = useTable({ request, columns: [], immediate: false }) as TableApi
          return () => h('div', [h(api.LmTable)])
        },
      },
      { attachTo: document.body },
    )
    await api.refresh()
    expect(request.mock.calls[0][0].sorter).toBeNull()

    // 经内层 NaiveLmTable 触发 update:sorter/update:filters(短名组件以监听器透传,非声明 props)
    const table = wrapper.findComponent(NaiveLmTable)
    expect(table.exists()).toBe(true)
    table.vm.$emit('update:sorter', { columnKey: 'name', order: 'ascend' })
    table.vm.$emit('update:filters', { status: ['active'] })
    await api.refresh()

    expect(request.mock.calls[1][0].sorter).toEqual({ columnKey: 'name', order: 'ascend' })
    expect(request.mock.calls[1][0].filters).toEqual({ status: ['active'] })
    wrapper.unmount()
  })
})

describe('useTable 搜索组件与 local 过滤', () => {
  it('LmSearch remote 模式:搜索写入 param 并发请求', async () => {
    const request = vi.fn().mockResolvedValue({ data: [], total: 0 })
    const { api, wrapper } = setupTableWithSearch({ request, columns: [], immediate: false })

    const search = wrapper.findComponent(NaiveLmTableSearch)
    expect(search.exists()).toBe(true)
    // onSearch 为 emit 监听器(useTable LmSearch 透传),经 $emit 触发
    search.vm.$emit('search', 'hello')
    await flush()

    expect(request).toHaveBeenCalledTimes(1)
    expect(request.mock.calls[0][0]).toMatchObject({ keyword: 'hello', page: 1 })
    expect(api.hasActiveQuery.value).toBe(true)
    wrapper.unmount()
  })

  it('LmSearch local 模式:关键字前端过滤,不发新请求', async () => {
    const rows: Row[] = [
      { id: 1, name: 'Alpha', status: 'on' },
      { id: 2, name: 'Beta', status: 'off' },
    ]
    const request = vi.fn().mockResolvedValue({ data: rows, total: rows.length })
    let api!: TableApi
    const wrapper = mount(
      {
        setup() {
          api = useTable({
            request,
            columns: [],
            immediate: false,
            config: { filter: { mode: 'local', fields: ['name'] } },
          }) as TableApi
          return () => h('div', [h(api.LmSearch), h(api.LmTable)])
        },
      },
      { attachTo: document.body },
    )
    await api.refresh()

    const search = wrapper.findComponent(NaiveLmTableSearch)
    search.vm.$emit('search', 'alpha')
    await nextTick()

    expect(request).toHaveBeenCalledTimes(1) // 未发新请求
    expect(api.hasActiveQuery.value).toBe(true)
    // data 保持全量,过滤结果在内层 LmTable 的 displayData 渲染
    expect(api.data.value).toHaveLength(2)
    wrapper.unmount()
  })

  it('selection.clearAfterRequest:请求后清空选择', async () => {
    const request = vi.fn().mockResolvedValue({ data: [{ id: 1, name: 'a', status: 'ok' }], total: 1 })
    const { api, wrapper } = setupTable({
      request,
      columns: [],
      immediate: false,
      config: { selection: { clearAfterRequest: true } },
    })

    await api.refresh()
    api.selectedKeys.value = [1]
    api.selectedRows.value = [{ id: 1, name: 'a', status: 'ok' }]
    await api.refresh()
    expect(api.selectedKeys.value).toHaveLength(0)
    expect(api.selectedRows.value).toHaveLength(0)
    wrapper.unmount()
  })

  it('clearSelection 清空 keys 与 rows', () => {
    const request = vi.fn().mockResolvedValue({ data: [], total: 0 })
    const { api, wrapper } = setupTable({ request, columns: [], immediate: false })
    api.selectedKeys.value = [1, 2]
    api.selectedRows.value = [
      { id: 1, name: 'a', status: 'ok' },
      { id: 2, name: 'b', status: 'ok' },
    ]
    api.clearSelection()
    expect(api.selectedKeys.value).toHaveLength(0)
    expect(api.selectedRows.value).toHaveLength(0)
    wrapper.unmount()
  })

  it('extension refreshList:source 置 true 触发刷新并自动复位', async () => {
    const request = vi.fn().mockResolvedValue({ data: [], total: 0 })
    const refreshList = ref(false)
    const { api: _api, wrapper } = setupTable({
      request,
      columns: [],
      immediate: false,
      extension: [{ type: 'refreshList', source: refreshList }],
    })
    void _api

    await flush()
    expect(request).not.toHaveBeenCalled()

    refreshList.value = true
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1))
    expect(refreshList.value).toBe(false)
    wrapper.unmount()
  })
})
