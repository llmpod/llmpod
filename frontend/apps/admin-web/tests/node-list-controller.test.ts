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
}))
vi.mock('@/api/gpu', () => ({ listGpus: vi.fn() }))

// 弹窗/行展开内容组件仅作占位:列定义断言只依赖组件引用与透传 props,不依赖其内部实现
vi.mock('@/views/node-list/NodeGpuExpand.vue', () => ({ default: { name: 'NodeGpuExpandStub' } }))
vi.mock('@/views/node-list/GpuInfoModal.vue', () => ({ default: {} }))
vi.mock('@/views/node-list/NodeRegisterModal.vue', () => ({ default: {} }))

// useTable 替换为桩以捕获列定义;useOperate 保留真实现(操作列 render 可断言)
const useTableMock = vi.fn()

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useTable: (...args: unknown[]) => useTableMock(...args),
    useDialog: vi.fn(),
    useEntryAction: vi.fn(),
  }
})

import { useController } from '@/views/node-list/useController'
import NodeGpuExpand from '@/views/node-list/NodeGpuExpand.vue'
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

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
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

  it('操作列:详情恒显(升级入口已随升级功能下线移除)', () => {
    mountController()
    const operate = tableOptions().columns.find(
      (col) => (col as unknown as Record<string, unknown>).__lmOperate === true,
    ) as unknown as { render: (row: ComputeNode) => VNode } | undefined
    expect(operate).toBeTruthy()

    const row = { id: 'n1' } as ComputeNode
    const vnode = operate!.render(row)
    const actions = (vnode.props as { actions: Array<{ title: string; show?: (row: ComputeNode) => boolean }> }).actions
    expect(actions.map((action) => action.title)).toEqual(['详情'])
  })

  it('版本列:未上报显示 —,同版显示版本号,落后附可升级标签', () => {
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
