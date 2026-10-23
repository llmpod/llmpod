// @vitest-environment happy-dom
import { createApp, h, nextTick, ref } from 'vue'
import type { VNode } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/cluster', () => ({
  listClusters: vi.fn(),
  createCluster: vi.fn(),
  updateCluster: vi.fn(),
  deleteCluster: vi.fn(),
}))
vi.mock('@/api/node', () => ({ listNodes: vi.fn() }))
vi.mock('@/api/gpu', () => ({ listGpus: vi.fn() }))
vi.mock('@/api/quick-setup', () => ({
  markClusterCreated: vi.fn(),
  resetClusterCache: vi.fn(),
}))
// useController 读取 route.query.topology 判断拓扑直达,mock 为可变 route 供用例注入 query
const { routeMock, routerReplace } = vi.hoisted(() => ({
  routeMock: { query: {} as Record<string, unknown> },
  routerReplace: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => ({ push: vi.fn(), replace: routerReplace }),
}))
vi.mock('@/views/node-pool/PoolForm.vue', () => ({ default: {} }))

// useTable 替换为桩以捕获列定义;useOperate 保留真实现(操作列 render 可断言)
const useTableMock = vi.fn()

vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useTable: (...args: unknown[]) => useTableMock(...args),
    useDialog: vi.fn(),
    useConfirm: vi.fn(),
    useEntryAction: vi.fn(),
  }
})

import { useController } from '@/views/node-pool/useController'
import type { NodePoolRecord } from '@/views/node-pool/useStore'

/** mock route 对象(用例注入 query 用) */
const route = routeMock

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
    columns: Array<Record<string, unknown>>
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  route.query = {}
  useTableMock.mockReturnValue({
    LmTable: {},
    LmPage: {},
    LmRefresh: {},
    LmColumn: {},
    refresh: vi.fn(),
    /** 模拟真实 useTable:immediate 请求首屏数据 */
    data: ref([]),
  })
})

describe('node-pool useController 表格列', () => {
  it('集群名称列可点击进入拓扑详情视图', () => {
    const { controller } = mountController()
    const name = tableOptions().columns.find(col => col.key === 'name') as unknown as {
      render: (row: NodePoolRecord) => VNode
    }
    expect(name).toBeTruthy()

    const row = { id: 1, name: 'gpu-shared' } as NodePoolRecord
    const vnode = name.render(row)
    expect(vnode.type).toBe('span')
    expect(vnode.children).toBe('gpu-shared')
    expect(vnode.props).toMatchObject({ class: 'text-primary cursor-pointer hover:opacity-[0.9]' })

    ;(vnode.props as { onClick: () => void }).onClick()
    expect(controller.topologyPool.value).toMatchObject({ id: 1 })
  })
})

describe('node-pool useController ?topology= 直达', () => {
  /** 覆盖 useTable 桩,返回可变的 data ref 模拟首屏数据回填(触发 data watch) */
  function makeTable() {
    const data = ref<Array<{ id: number; name: string }>>([])
    useTableMock.mockImplementationOnce(() => ({
      LmTable: {},
      LmPage: {},
      LmRefresh: {},
      LmColumn: {},
      refresh: vi.fn(),
      data,
    }))
    return data
  }

  it('?topology=first 首屏数据到达后展开首行拓扑,并清掉 query', async () => {
    route.query = { topology: 'first' }
    const data = makeTable()
    const { controller } = mountController()

    data.value = [{ id: 7, name: 'pool-a' }, { id: 8, name: 'pool-b' }]
    await nextTick()

    expect(controller.topologyPool.value).toMatchObject({ id: 7 })
    expect(routerReplace).toHaveBeenCalledWith({ query: {} })
  })

  it('?topology=<clusterId> 展开指定集群拓扑(运营大屏跳转),并清掉 query', async () => {
    route.query = { topology: '8' }
    const data = makeTable()
    const { controller } = mountController()

    data.value = [{ id: 7, name: 'pool-a' }, { id: 8, name: 'pool-b' }]
    await nextTick()

    expect(controller.topologyPool.value).toMatchObject({ id: 8 })
    expect(routerReplace).toHaveBeenCalledWith({ query: {} })
  })

  it('?topology= 在途期间以骨架占位替代表格,避免先闪列表再切拓扑', async () => {
    route.query = { topology: '8' }
    const data = makeTable()
    const { controller } = mountController()

    // 列表在途:entryPending 置位,模板据此渲染骨架占位而非表格
    expect(controller.entryPending.value).toBe(true)

    data.value = [{ id: 7, name: 'pool-a' }, { id: 8, name: 'pool-b' }]
    await nextTick()

    expect(controller.entryPending.value).toBe(false)
    expect(controller.topologyPool.value).toMatchObject({ id: 8 })
  })

  it('?topology=<clusterId> 目标集群不在列表时停留表格,仍清 query 避免重复触发', async () => {
    route.query = { topology: '99' }
    const data = makeTable()
    const { controller } = mountController()

    data.value = [{ id: 7, name: 'pool-a' }]
    await nextTick()

    expect(controller.topologyPool.value).toBeNull()
    expect(routerReplace).toHaveBeenCalledWith({ query: {} })
  })

  it('无 topology 参数时不进入拓扑视图', async () => {
    const data = makeTable()
    const { controller } = mountController()

    data.value = [{ id: 7, name: 'pool-a' }]
    await nextTick()

    expect(controller.topologyPool.value).toBeNull()
    expect(routerReplace).not.toHaveBeenCalled()
  })
})
