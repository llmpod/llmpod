// @vitest-environment happy-dom
/**
 * 模型节点副本的「后端契约对齐」验收:用后端真实字段名(node_count / local_dir / bytes_total /
 * last_checked_at / present|absent|downloading|verifying|failed)断言渲染结果,
 * 防止前端再照需求文档自造字段名导致三列全空、状态漏翻。
 */
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

import { listModelNodes } from '@/api/model'
import {
  formatBytes,
  formatDateTime,
  MODEL_NODE_STATUS_LABEL,
  MODEL_NODE_STATUS_TAG,
  type ModelNodeCopy,
} from '@/views/model-management/useStore'
import ModelNodesDialog from '@/views/model-management/ModelNodesDialog.vue'
import DeleteNodeCopyDialog from '@/views/model-management/DeleteNodeCopyDialog.vue'

/** 后端 GET /models/{id}/nodes 的真实返回形状(字段名以服务端为准) */
function backendNode(overrides: Partial<ModelNodeCopy> = {}): ModelNodeCopy {
  return {
    node_id: 'node-a',
    hostname: 'gpu-a',
    ip: '10.0.0.11',
    local_dir: '/data/models/qwen2.5-7b',
    node_status: 'online',
    model_status: 'present',
    bytes_total: 15_998_222_336,
    last_checked_at: '2026-11-01T10:00:00Z',
    ...overrides,
  }
}

/** 取弹窗表格 columns(useTable 桩捕获) */
function dialogColumns() {
  const options = useTableMock.mock.calls[0][0] as {
    columns: Array<{ title?: string; key?: string; render?: (row: ModelNodeCopy) => unknown }>
  }
  return options.columns
}

function byKey(key: string) {
  const col = dialogColumns().find((item) => item.key === key)
  expect(col, `列 ${key}`).toBeTruthy()
  return col!
}

/** 从 VNode 收集文本 */
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

beforeEach(() => {
  vi.clearAllMocks()
  useTableMock.mockReturnValue({
    LmTable: {},
    LmRefresh: {},
    total: 1,
    refresh: vi.fn(),
  })
  vi.mocked(listModelNodes).mockResolvedValue([backendNode()])
})

describe('节点副本弹窗:列取后端字段名(不是需求文档里的自造名)', () => {
  it('列表请求打 /models/{id}/nodes', async () => {
    mount(ModelNodesDialog, { props: { modelId: 3, modelName: 'Qwen2.5-7B' } })
    // useTable 被桩替换后不会自动发请求,这里手动触发捕获到的 request
    const options = useTableMock.mock.calls[0][0] as { request: () => Promise<unknown> }
    await options.request()
    expect(listModelNodes).toHaveBeenCalled()
    expect(vi.mocked(listModelNodes).mock.calls[0][0]).toBe(3)
  })

  it('路径列读 local_dir(不是 path)', () => {
    mount(ModelNodesDialog, { props: { modelId: 3, modelName: 'Qwen2.5-7B' } })
    const pathCol = byKey('local_dir')
    expect(collectText(pathCol.render!(backendNode()))).toContain('/data/models/qwen2.5-7b')
    // 后端不再下发 path,不应再有这一列
    expect(dialogColumns().some((col) => col.key === 'path')).toBe(false)
  })

  it('大小列读 bytes_total 并换算 GB(不是 size_gb)', () => {
    mount(ModelNodesDialog, { props: { modelId: 3, modelName: 'Qwen2.5-7B' } })
    const sizeCol = byKey('bytes_total')
    expect(collectText(sizeCol.render!(backendNode()))).toBe('14.90 GB')
    expect(dialogColumns().some((col) => col.key === 'size_gb')).toBe(false)
  })

  it('最近校验时间列读 last_checked_at(不是 last_verified_at)', () => {
    mount(ModelNodesDialog, { props: { modelId: 3, modelName: 'Qwen2.5-7B' } })
    const timeCol = byKey('last_checked_at')
    expect(collectText(timeCol.render!(backendNode()))).toBe(
      formatDateTime('2026-11-01T10:00:00Z'),
    )
    expect(dialogColumns().some((col) => col.key === 'last_verified_at')).toBe(false)
  })

  it('模型状态列按后端词表翻译成中文,不出现英文原文', () => {
    mount(ModelNodesDialog, { props: { modelId: 3, modelName: 'Qwen2.5-7B' } })
    const statusCol = byKey('model_status')
    const cases: [string, string][] = [
      ['present', '已就绪'],
      ['absent', '未下载'],
      ['downloading', '下载中'],
      ['verifying', '校验中'],
      ['failed', '失败'],
    ]
    for (const [value, label] of cases) {
      const text = collectText(statusCol.render!(backendNode({ model_status: value })))
      expect(text, `model_status=${value}`).toContain(label)
      expect(text, `model_status=${value} 不应显示英文原文`).not.toContain(value)
    }
  })

  it('模型状态配色覆盖后端五个取值', () => {
    expect(Object.keys(MODEL_NODE_STATUS_TAG).sort()).toEqual(
      ['absent', 'downloading', 'failed', 'present', 'verifying'].sort(),
    )
    expect(Object.keys(MODEL_NODE_STATUS_LABEL).sort()).toEqual(
      ['absent', 'downloading', 'failed', 'present', 'verifying'].sort(),
    )
  })
})

describe('删除副本弹窗:展示 local_dir', () => {
  it('弹窗内「路径」与风险提示都用 local_dir', () => {
    const wrapper = mount(DeleteNodeCopyDialog, {
      props: { modelId: 3, node: backendNode() },
    })
    const text = wrapper.text()
    expect(text).toContain('/data/models/qwen2.5-7b')
    expect(text).not.toContain('undefined')
  })
})

describe('formatBytes 边界', () => {
  it('未下发(undefined/null)显示 --', () => {
    expect(formatBytes(undefined)).toBe('--')
    expect(formatBytes(null)).toBe('--')
  })
})
