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
  formatBytes,
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
  const column = options.columns.find((col) => col.key === 'node_count') as
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
    local_dir: '/data/models/qwen2.5-7b',
    node_status: 'online',
    model_status: 'present',
    bytes_total: 15_998_222_336, // ≈14.90 GB
    last_checked_at: '2026-11-01T10:00:00Z',
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

  it('节点状态与模型状态是两套独立映射(需分开展示,词表对齐后端)', () => {
    expect(NODE_STATUS_LABEL.online).toBe('在线')
    expect(NODE_STATUS_LABEL.offline).toBe('离线')
    // 模型状态词表为后端的 present/absent/downloading/verifying/failed
    expect(MODEL_NODE_STATUS_LABEL.present).toBe('已就绪')
    expect(MODEL_NODE_STATUS_LABEL.absent).toBe('未下载')
    expect(MODEL_NODE_STATUS_LABEL.downloading).toBe('下载中')
    expect(MODEL_NODE_STATUS_LABEL.verifying).toBe('校验中')
    expect(MODEL_NODE_STATUS_LABEL.failed).toBe('失败')
    expect(NODE_STATUS_TAG.online).toBe('success')
    expect(MODEL_NODE_STATUS_TAG.present).toBe('success')
    // 键不交叉:节点状态映射里没有 present
    expect(NODE_STATUS_TAG.present).toBeUndefined()
    // 后端不会下发这些历史词,不应残留映射(否则会漏翻成英文原文)
    expect(MODEL_NODE_STATUS_LABEL.cached).toBeUndefined()
    expect(MODEL_NODE_STATUS_LABEL.ready).toBeUndefined()
    expect(MODEL_NODE_STATUS_LABEL.missing).toBeUndefined()
    expect(MODEL_NODE_STATUS_LABEL.corrupted).toBeUndefined()
  })
})

describe('formatBytes(模型副本大小列:后端下发 bytes_total)', () => {
  it('按量级换算 GB / MB / KB / B', () => {
    expect(formatBytes(15_998_222_336)).toBe('14.90 GB')
    expect(formatBytes(1024 ** 3)).toBe('1.00 GB')
    expect(formatBytes(5 * 1024 ** 2)).toBe('5.0 MB')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(512)).toBe('512 B')
  })

  it('空值/非法值为 --', () => {
    expect(formatBytes(null)).toBe('--')
    expect(formatBytes(undefined)).toBe('--')
    expect(formatBytes(Number.NaN)).toBe('--')
    expect(formatBytes(-1)).toBe('--')
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

  it('后端 node_count 渲染「已下载节点：N」;点击打开节点副本弹窗', () => {
    const { wrapper } = mountController()
    const column = nodeColumn()
    expect(column.title).toBe('已下载节点')

    const vnode = column.render(makeNodeRecord({ node_count: 2 }))
    expect(collectText(vnode)).toContain('已下载节点：2')

    vnode.props.onClick()
    const call = vi.mocked(useDialog).mock.calls[0][0]
    expect(call.title).toContain('已下载节点')
    expect(call.props).toMatchObject({ modelId: 3, modelName: 'Qwen2.5-7B' })
    wrapper.unmount()
  })

  it('node_count=0 显示「未下载」;未下发(undefined)显示 --', () => {
    const { wrapper } = mountController()
    const column = nodeColumn()

    expect(collectText(column.render(makeNodeRecord({ node_count: 0 })))).toContain('未下载')
    expect(collectText(column.render(makeNodeRecord()))).toContain('--')
    wrapper.unmount()
  })
})