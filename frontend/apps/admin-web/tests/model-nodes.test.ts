// @vitest-environment happy-dom
import { h } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/model', () => ({
  listModels: vi.fn(),
  inspectModelSource: vi.fn(),
  createModel: vi.fn(),
  updateModel: vi.fn(),
  deleteModel: vi.fn(),
  listModelNodes: vi.fn(),
  deleteModelNode: vi.fn(),
}))

/** useTable 桩:捕获列定义,列表装配不参与本组测试 */
const useTableMock = vi.fn()
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useTable: (...args: unknown[]) => useTableMock(...args),
    useDialog: vi.fn(),
    useConfirm: vi.fn(),
    useEntryAction: vi.fn(),
    message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  }
})

import { deleteModelNode, listModelNodes } from '@/api/model'
import { useDialog } from '@repo/hooks'
import {
  MODEL_NODE_STATUS_LABEL,
  MODEL_NODE_STATUS_TAG,
  NODE_STATUS_LABEL,
  NODE_STATUS_TAG,
  useStore,
  type ModelNodeCopy,
  type ModelRecord,
} from '@/views/model-management/useStore'
import { useController } from '@/views/model-management/useController'

const store = useStore()

/** 从 VNode 收集文本(兼容 JSX 的 { default: () => [...] } 子节点) */
function collectText(node: unknown): string {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(collectText).join('')
  const vnode = node as { children?: unknown }
  const children = vnode.children
  if (children && typeof children === 'object' && !Array.isArray(children)) {
    const slot = (children as { default?: () => unknown }).default
    if (typeof slot === 'function') return collectText(slot())
  }
  return collectText(children)
}

/** useController 收到的列定义里的「已下载节点」列 */
function nodeColumn() {
  const options = useTableMock.mock.calls[0][0] as {
    columns: Array<Record<string, unknown>>
  }
  const column = options.columns.find((col) => col.key === 'cached_node_count') as
    | {
        title: string
        render: (row: ModelRecord) => { props: { onClick: () => void }; children: unknown }
      }
    | undefined
  expect(column, '已下载节点列').toBeTruthy()
  return column!
}

function makeNodeRecord(overrides: Partial<ModelRecord> = {}): ModelRecord {
  return {
    id: 3,
    name: 'Qwen2.5-7B',
    source: 'huggingface',
    source_url: 'Qwen/Qwen2.5-7B-Instruct',
    revision: null,
    model_type: 'llm',
    format: 'safetensors',
    architecture: null,
    size_gb: 14.5,
    quantization: null,
    is_moe: false,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  } as ModelRecord
}

function makeNodeCopy(overrides: Partial<ModelNodeCopy> = {}): ModelNodeCopy {
  return {
    node_id: 'node-1',
    hostname: 'gpu-a',
    ip: '10.0.0.11',
    path: '/data/models/qwen2.5-7b',
    node_status: 'online',
    model_status: 'cached',
    size_gb: 14.5,
    last_verified_at: '2026-11-01T10:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useTableMock.mockReturnValue({
    LmTable: {},
    LmPage: {},
    LmRefresh: {},
    LmColumn: {},
    refresh: vi.fn(),
  })
})

describe('模型节点副本 store(第 8 点)', () => {
  it('nodes 透传 modelId 与 AbortSignal;removeNodeCopy 透传 delete_files', async () => {
    vi.mocked(listModelNodes).mockResolvedValue([makeNodeCopy()])
    const controller = new AbortController()
    await expect(store.nodes(3, controller.signal)).resolves.toHaveLength(1)
    expect(listModelNodes).toHaveBeenCalledWith(3, controller.signal)

    vi.mocked(deleteModelNode).mockResolvedValue(undefined)
    await store.removeNodeCopy(3, 'node-1', false)
    expect(deleteModelNode).toHaveBeenCalledWith(3, 'node-1', false)

    // 删除成功后 notify 联动列表刷新
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    await store.removeNodeCopy(3, 'node-2', true)
    expect(deleteModelNode).toHaveBeenLastCalledWith(3, 'node-2', true)
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('节点状态与模型状态是两套独立映射(需分开展示)', () => {
    expect(NODE_STATUS_LABEL.online).toBe('在线')
    expect(NODE_STATUS_LABEL.offline).toBe('离线')
    expect(MODEL_NODE_STATUS_LABEL.cached).toBe('已缓存')
    expect(MODEL_NODE_STATUS_LABEL.downloading).toBe('下载中')
    expect(NODE_STATUS_TAG.online).toBe('success')
    expect(MODEL_NODE_STATUS_TAG.cached).toBe('success')
    // 键不交叉:节点状态映射里没有 cached
    expect(NODE_STATUS_TAG.cached).toBeUndefined()
  })
})

describe('模型列表「已下载节点」列(第 8 点)', () => {
  function mountController() {
    let controller!: ReturnType<typeof useController>
    const wrapper = mount({
      setup: () => {
        controller = useController()
        return () => h('div')
      },
    })
    return { controller, wrapper }
  }

  it('后端计数优先 → 「已下载节点：N」;点击打开节点副本弹窗', () => {
    const { wrapper } = mountController()
    const column = nodeColumn()
    expect(column.title).toBe('已下载节点')

    const vnode = column.render(makeNodeRecord({ cached_node_count: 2 }))
    expect(collectText(vnode)).toContain('已下载节点：2')

    vnode.props.onClick()
    const call = vi.mocked(useDialog).mock.calls[0][0]
    expect(call.title).toContain('已下载节点')
    expect(call.props).toMatchObject({ modelId: 3, modelName: 'Qwen2.5-7B' })
    wrapper.unmount()
  })

  it('计数缺失回退 cached_nodes 节点数;两者都缺显示 --(后端未下发)', () => {
    const { wrapper } = mountController()
    const column = nodeColumn()

    expect(
      collectText(column.render(makeNodeRecord({ cached_nodes: { a: '/a', b: '/b' } }))),
    ).toContain('已下载节点：2')
    expect(collectText(column.render(makeNodeRecord({ cached_node_count: 0 })))).toContain(
      '未下载',
    )
    expect(collectText(column.render(makeNodeRecord()))).toContain('--')
    wrapper.unmount()
  })
})