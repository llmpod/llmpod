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
}))

const routerPushMock = vi.hoisted(() => vi.fn())
vi.mock('vue-router', () => ({ useRouter: () => ({ push: routerPushMock }) }))

vi.mock('@/views/model-management/add-model/AddModelDialog.vue', () => ({ default: {} }))
vi.mock('@/views/model-management/EditModelDialog.vue', () => ({ default: {} }))
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
    message: { success: vi.fn(), error: vi.fn() },
  }
})

import { createModel, deleteModel, inspectModelSource, listModels, updateModel } from '@/api/model'
import { useDialog } from '@repo/hooks'
import type { LmSelectAction } from '@repo/naive-ui'
import {
  formatDateTime,
  MODEL_TYPE_OPTIONS,
  SOURCE_LABEL,
  SOURCE_OPTIONS,
  SOURCE_URL_PLACEHOLDER,
  TYPE_TAG,
  typeLabel,
  useStore,
  type ModelFormValues,
  type ModelRecord,
} from '@/views/model-management/useStore'
import { useController } from '@/views/model-management/useController'

const store = useStore()

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

function makeForm(overrides: Partial<ModelFormValues> = {}): ModelFormValues {
  return {
    name: 'Qwen2.5-7B',
    source: 'huggingface',
    source_url: 'Qwen/Qwen2.5-7B-Instruct',
    revision: '',
    model_type: 'llm',
    ...overrides,
  }
}

describe('model-management useStore', () => {
  it('request 分页透传 page_size 与 AbortSignal', async () => {
    vi.mocked(listModels).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 50,
      total_pages: 0,
    } as Awaited<ReturnType<typeof listModels>>)
    const controller = new AbortController()
    await store.request({ page: 2, pageSize: 20, page_size: 50 } as { page: number; pageSize: number }, { signal: controller.signal })
    expect(listModels).toHaveBeenCalledWith({ page: 2, page_size: 50 }, controller.signal)
  })

  it('inspect 透传探测请求', async () => {
    vi.mocked(inspectModelSource).mockResolvedValue({ files: { success: true }, feasibility: null } as Awaited<ReturnType<typeof inspectModelSource>>)
    const payload = { source: 'huggingface', source_url: 'Qwen/Qwen2.5-7B', revision: null }
    const result = await store.inspect(payload as Parameters<typeof store.inspect>[0])
    expect(inspectModelSource).toHaveBeenCalledWith(payload)
    expect(result.files.success).toBe(true)
  })

  it('create:files 原样回传,revision 空串转 null,创建后 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    const files = { success: true, format: 'safetensors' }
    vi.mocked(createModel).mockResolvedValue({ id: 1 } as Awaited<ReturnType<typeof createModel>>)

    await store.create(makeForm(), files as Parameters<typeof store.create>[1])
    expect(createModel).toHaveBeenCalledWith({
      name: 'Qwen2.5-7B',
      source: 'huggingface',
      source_url: 'Qwen/Qwen2.5-7B-Instruct',
      revision: null,
      model_type: 'llm',
      files,
    })
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('update:PATCH 语义空值转 null,is_moe 默认 false', async () => {
    vi.mocked(updateModel).mockResolvedValue(undefined)
    await store.update(makeForm({ id: 7, revision: '', format: undefined, architecture: '', size_gb: undefined, quantization: undefined, is_moe: undefined }))
    expect(updateModel).toHaveBeenCalledWith(7, {
      name: 'Qwen2.5-7B',
      model_type: 'llm',
      source: 'huggingface',
      source_url: 'Qwen/Qwen2.5-7B-Instruct',
      revision: null,
      format: null,
      architecture: null,
      size_gb: null,
      quantization: null,
      is_moe: false,
    })
  })

  it('remove 删除并 notify', async () => {
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    vi.mocked(deleteModel).mockResolvedValue(undefined)
    await store.remove(7)
    expect(deleteModel).toHaveBeenCalledWith(7)
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('typeLabel:已知类别取文案,未知原样展示', () => {
    expect(typeLabel('llm')).toBe('LLM')
    expect(typeLabel('multimodal')).toBe('多模态')
    expect(typeLabel('custom-x')).toBe('custom-x')
  })

  it('选项与映射常量', () => {
    // OSS 源暂时隐藏(useStore 中注释),选项仅剩 huggingface/local
    expect(SOURCE_OPTIONS.map((item) => item.value)).toEqual(['huggingface', 'local'])
    expect(SOURCE_URL_PLACEHOLDER.local).toContain('/data/models')
    expect(SOURCE_LABEL.local).toBe('本地')
    expect(MODEL_TYPE_OPTIONS.length).toBeGreaterThan(5)
    expect(TYPE_TAG.llm.textColor).toContain('var(--n-primary-color)')
    expect(formatDateTime(null)).toBe('—')
  })
})

// ---------- controller: 行操作「新建部署」跳转 ----------

function makeModel(overrides: Partial<ModelRecord> = {}): ModelRecord {
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

/** useTable 收到的 options(列定义断言入口) */
function tableOptions() {
  expect(useTableMock).toHaveBeenCalledTimes(1)
  return useTableMock.mock.calls[0][0] as {
    columns: Array<Record<string, unknown>>
  }
}

/** 操作列 render 产物中的行操作项(真实 onClick 编排入口) */
function operateActions(): LmSelectAction<ModelRecord>[] {
  const operate = tableOptions().columns.find((col) => col.__lmOperate === true) as unknown as {
    render: (row: ModelRecord) => { props: { actions: LmSelectAction<ModelRecord>[] } }
  }
  expect(operate).toBeTruthy()
  return operate.render(makeModel()).props.actions
}

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

describe('model-management useController', () => {
  it('openAdd 经 useDialog 打开添加模型向导', () => {
    const { controller, wrapper } = mountController()
    controller.openAdd()
    expect(useDialog).toHaveBeenCalledTimes(1)
    const call = vi.mocked(useDialog).mock.calls[0][0]
    expect(call.title).toBe('添加模型')
    wrapper.unmount()
  })

  it('行操作「新建部署」:跳转部署列表 tab 并携带 action=deploy 与模型 id(独立键避开 management tab 的 create=添加模型)', () => {
    const { wrapper } = mountController()

    const deployAction = operateActions().find((action) => action.title === '新建部署')!
    expect(deployAction).toBeTruthy()
    deployAction.onClick!(makeModel({ id: 7, name: 'llama-3-8b' }))

    expect(routerPushMock).toHaveBeenCalledWith({
      name: 'deploy',
      query: { tab: 'list', action: 'deploy', model: '7' },
    })
    wrapper.unmount()
  })

  it('行操作仍含 编辑 / 删除,顺序为 编辑→新建部署→删除', () => {
    const { wrapper } = mountController()
    const titles = operateActions().map((action) => action.title)
    expect(titles).toEqual(['编辑', '新建部署', '删除'])
    wrapper.unmount()
  })
})
