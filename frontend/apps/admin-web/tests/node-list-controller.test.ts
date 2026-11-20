// @vitest-environment happy-dom
import { createApp, h } from 'vue'
import type { VNode } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DataTableColumn } from 'naive-ui'

vi.mock('@/api/node', () => ({
  listNodes: vi.fn(),
  getNode: vi.fn(),
  updateNode: vi.fn(),
  deleteNode: vi.fn(),
  upgradeNode: vi.fn(),
}))
vi.mock('@/api/gpu', () => ({ listGpus: vi.fn() }))

// 弹窗/行展开内容组件仅作占位:列定义断言只依赖组件引用与透传 props,不依赖其内部实现
vi.mock('@/views/node-list/NodeGpuExpand.vue', () => ({ default: { name: 'NodeGpuExpandStub' } }))
vi.mock('@/views/node-list/GpuInfoModal.vue', () => ({ default: {} }))
vi.mock('@/views/node-list/NodeRegisterModal.vue', () => ({ default: {} }))

// useTable 替换为桩以捕获列定义;useOperate 保留真实现(操作列 render 可断言)
// 桩须 vi.hoisted:vi.mock 工厂会随 import 链提前执行,顶层 const 此时尚未初始化
const { useTableMock, setDataMock, confirmMock, messageMock } = vi.hoisted(() => ({
  useTableMock: vi.fn(),
  setDataMock: vi.fn(),
  /** 二次确认桩:默认通过;置为 reject 可模拟用户取消 */
  confirmMock: vi.fn(),
  messageMock: { success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useTable: (...args: unknown[]) => useTableMock(...args),
    useDialog: vi.fn(),
    useEntryAction: vi.fn(),
    useConfirm: (...args: unknown[]) => confirmMock(...args),
    message: messageMock,
  }
})

import { useController } from '@/views/node-list/useController'
import NodeGpuExpand from '@/views/node-list/NodeGpuExpand.vue'
import { listNodes, upgradeNode } from '@/api/node'
import { listGpus } from '@/api/gpu'
import type { ComputeNode } from '@/views/node-list/useStore'

/** 在真实组件 setup 内创建控制器(触发 onBeforeUnmount 等生命周期) */
function mountController() {
  let controller!: ReturnType<typeof useController>
  const app = createApp({
    setup: () => {
      controller = useController()
      return () => h('div')
    },
  })
  app.mount(document.createElement('div'))
  return { controller, unmount: () => app.unmount() }
}

/** useTable 收到的 options(列定义断言入口) */
function tableOptions() {
  expect(useTableMock).toHaveBeenCalledTimes(1)
  return useTableMock.mock.calls[0][0] as {
    columns: Array<DataTableColumn<ComputeNode>>
  }
}

/** 一轮节点列表响应(静默轮询测试用) */
function nodePage(items: unknown[], total: number) {
  return { items, total, page: 1, page_size: 10, total_pages: 1 } as Awaited<ReturnType<typeof listNodes>>
}

/** 一轮 GPU 列表响应(静默轮询测试用) */
function gpuPage(items: unknown[]) {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 100,
    total_pages: 1,
  } as Awaited<ReturnType<typeof listGpus>>
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  confirmMock.mockResolvedValue(undefined)
  useTableMock.mockReturnValue({
    LmTable: {},
    LmPage: {},
    LmRefresh: {},
    LmColumn: {},
    LmSearch: {},
    refresh: vi.fn(),
    reset: vi.fn(),
    hasActiveQuery: { value: false },
    data: { value: [] },
    loading: { value: false },
    setData: setDataMock,
  })
})

describe('node-list useController 表格列', () => {
  it('首列为行展开列,renderExpand 用行聚合 GPU 渲染 NodeGpuExpand', () => {
    mountController()
    const columns = tableOptions().columns
    const expand = columns[0] as unknown as {
      type?: string
      renderExpand?: (row: ComputeNode) => VNode
    }
    expect(expand.type).toBe('expand')

    const gpus = [{ id: 1, index: 0 }] as ComputeNode['gpus']
    const row = { id: 'n1', gpus } as ComputeNode
    const vnode = expand.renderExpand!(row)
    expect(vnode.type).toBe(NodeGpuExpand)
    expect(vnode.props).toMatchObject({ gpus })
  })

  it('操作列:详情恒显,升级只在落后管理端版本时出现', () => {
    mountController()
    const operate = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).__lmOperate === true,
    ) as unknown as { render: (row: ComputeNode) => VNode } | undefined
    expect(operate).toBeTruthy()

    const row = { id: 'n1' } as ComputeNode
    const vnode = operate!.render(row)
    const actions = (vnode.props as { actions: Array<{ title: string; show?: (row: ComputeNode) => boolean }> }).actions
    expect(actions.map((action) => action.title)).toEqual(['详情', '升级'])

    const upgrade = actions.find((action) => action.title === '升级')!
    expect(upgrade.show!({ version_status: 'outdated' } as ComputeNode)).toBe(true)
    // 同版/超前/未上报都没有升级入口
    expect(upgrade.show!({ version_status: 'ok' } as ComputeNode)).toBe(false)
    expect(upgrade.show!({ version_status: 'newer' } as ComputeNode)).toBe(false)
    expect(upgrade.show!({ version_status: 'unknown' } as ComputeNode)).toBe(false)
  })

  it('onUpgrade:二次确认后下发升级;未下发时后端中文 reason 原样透出', async () => {
    const { controller } = mountController()
    const row = { id: 'n1', hostname: 'gpu-01', version_status: 'outdated' } as ComputeNode

    vi.mocked(upgradeNode).mockResolvedValue({ id: 'n1', dispatched: true, reason: null })
    await controller.onUpgrade(row)
    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(upgradeNode).toHaveBeenCalledWith('n1')
    expect(messageMock.success).toHaveBeenCalled()

    vi.mocked(upgradeNode).mockResolvedValue({ id: 'n1', dispatched: false, reason: '节点不在线' })
    await controller.onUpgrade(row)
    expect(messageMock.warning).toHaveBeenCalledWith('节点不在线')
  })

  it('onUpgrade:取消二次确认不下发', async () => {
    const { controller } = mountController()
    confirmMock.mockRejectedValue(new Error('cancel'))
    await controller.onUpgrade({ id: 'n1', hostname: 'gpu-01' } as ComputeNode)
    expect(upgradeNode).not.toHaveBeenCalled()
  })

  it('静默轮询:10s 后重放当前页节点请求,不触发 useTable.refresh、不重复拉全量 GPU', async () => {
    vi.useFakeTimers()
    try {
      mountController()
      const options = tableOptions() as unknown as {
        request: (params: { page: number, pageSize: number, keyword: string }, ctx: { signal: AbortSignal }) => Promise<unknown>
      }
      vi.mocked(listNodes).mockResolvedValue(nodePage([], 0))
      vi.mocked(listGpus).mockResolvedValue(gpuPage([]))

      // 尚无查询参数(首屏未发出)时不请求
      await vi.advanceTimersByTimeAsync(10000)
      expect(listNodes).not.toHaveBeenCalled()

      // 首屏请求记录页码后,下个周期静默重放同一页
      await options.request({ page: 1, pageSize: 10, keyword: '' }, { signal: new AbortController().signal })
      vi.mocked(listNodes).mockClear()
      vi.mocked(listGpus).mockClear()
      // 数据变化(空页 → 一个节点)才会写回表格
      vi.mocked(listNodes).mockResolvedValue(nodePage([{ id: 'n1', hostname: 'gpu-01', ip: '10.0.0.1', status: 'online', runtime: null }], 1))
      await vi.advanceTimersByTimeAsync(10000)

      // 轮询带自己的 AbortSignal(卸载/新一轮可中止),失败静默不弹统一错误提示
      expect(listNodes).toHaveBeenCalledWith(
        { page: 1, page_size: 10 },
        expect.any(AbortSignal),
        { silentError: true },
      )
      // 第 1 轮不是 GPU 补拉周期:复用 store 缓存,只付节点接口的流量
      expect(listGpus).not.toHaveBeenCalled()
      expect(setDataMock).toHaveBeenCalledTimes(1)
      expect(useTableMock.mock.results[0].value.refresh).not.toHaveBeenCalled()
    }
    finally {
      vi.useRealTimers()
    }
  })

  it('静默轮询:数据与上一轮一致时跳过 setData,不白付一次整表重渲', async () => {
    vi.useFakeTimers()
    try {
      mountController()
      const options = tableOptions() as unknown as {
        request: (params: { page: number, pageSize: number, keyword: string }, ctx: { signal: AbortSignal }) => Promise<unknown>
      }
      const node = { id: 'n1', hostname: 'gpu-01', ip: '10.0.0.1', status: 'online', runtime: null }
      const gpu = {
        id: 1,
        uuid: 'g1',
        node_id: 'n1',
        index: 0,
        name: 'A100',
        total_vram_gb: 80,
        driver_version: '570',
        status: 'online',
        runtime: null,
      }
      vi.mocked(listNodes).mockResolvedValue(nodePage([node], 1))
      vi.mocked(listGpus).mockResolvedValue(gpuPage([gpu]))

      // 首屏请求:确立指纹基线(写回由 useTable 自己负责)
      await options.request({ page: 1, pageSize: 10, keyword: '' }, { signal: new AbortController().signal })
      vi.mocked(listNodes).mockClear()

      // 轮询拿到完全一致的数据:请求照发,但不写回表格
      await vi.advanceTimersByTimeAsync(10000)
      expect(listNodes).toHaveBeenCalledTimes(1)
      expect(setDataMock).not.toHaveBeenCalled()

      // 指标真的变了才写回
      vi.mocked(listNodes).mockResolvedValue(nodePage([{ ...node, runtime: { cpu_percent: 12 } }], 1))
      await vi.advanceTimersByTimeAsync(10000)
      expect(setDataMock).toHaveBeenCalledTimes(1)
    }
    finally {
      vi.useRealTimers()
    }
  })

  it('版本列:未上报显示 —,同版显示版本号,落后附可点击的 primary 升级标签', async () => {
    mountController()
    const version = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).key === 'version',
    ) as unknown as { render: (row: ComputeNode) => VNode }
    expect(version).toBeTruthy()

    // 未上报(version null / 存量节点) → 占位 —
    expect(version.render({ id: 'n1', version: null } as ComputeNode).children).toBe('—')
    // 同版 → 纯版本号文本
    expect(version.render({ id: 'n1', version: '26.9.3', version_status: 'ok' } as ComputeNode).children).toBe('26.9.3')
    // 落后 → 版本号 + 状态标签(两个子节点)
    const outdated = version.render({ id: 'n1', version: '26.9.2', version_status: 'outdated' } as ComputeNode)
    const children = outdated.children as VNode[]
    expect(children).toHaveLength(2)
    expect(children[0].children).toBe('26.9.2')

    // 可升级标签:primary 高亮 + 可点击(点击即走升级逻辑:二次确认 → 下发)
    const tag = children[1]
    const tagProps = tag.props as { type: string; class: string, onClick?: () => void }
    expect(tagProps.type).toBe('primary')
    expect(tagProps.class).toContain('cursor-pointer')

    vi.mocked(upgradeNode).mockResolvedValue({ id: 'n1', dispatched: true, reason: null })
    tagProps.onClick!()
    await vi.waitFor(() => expect(upgradeNode).toHaveBeenCalledWith('n1'))
    expect(confirmMock).toHaveBeenCalledTimes(1)
  })

  it('版本列:超前/未上报标签保持映射色且不可点', () => {
    mountController()
    const version = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).key === 'version',
    ) as unknown as { render: (row: ComputeNode) => VNode }

    const newer = version.render({ id: 'n1', version: '26.9.9', version_status: 'newer' } as ComputeNode)
    const tagProps = (newer.children as VNode[])[1].props as { type: string, onClick?: () => void }
    expect(tagProps.type).toBe('info')
    expect(tagProps.onClick).toBeUndefined()
  })

  it('openGpuDrawer 仍暴露给卡片模式(切换抽屉显隐)', () => {
    const { controller } = mountController()
    expect(controller.gpuDrawerShow.value).toBe(false)
    controller.openGpuDrawer({ id: 'n1' } as ComputeNode)
    expect(controller.activeNode.value).toMatchObject({ id: 'n1' })
    expect(controller.gpuDrawerShow.value).toBe(true)
  })

  it('主机名列可点击打开详情抽屉', () => {
    const { controller } = mountController()
    const hostname = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).key === 'hostname',
    ) as unknown as { render: (row: ComputeNode) => VNode }
    const row = { id: 'n1', hostname: 'gpu-01' } as ComputeNode

    const vnode = hostname.render(row)
    expect(vnode.type).toBe('span')
    expect(vnode.children).toBe('gpu-01')
    expect(vnode.props).toMatchObject({ class: 'text-primary cursor-pointer hover:opacity-[0.9]' })

    ;(vnode.props as { onClick: () => void }).onClick()
    expect(controller.activeNode.value).toMatchObject({ id: 'n1' })
    expect(controller.detailDrawerShow.value).toBe(true)
  })
})
