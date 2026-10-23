// @vitest-environment happy-dom
import { h, nextTick, reactive } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/model', () => ({ validateManifest: vi.fn() }))
vi.mock('@/views/model-management/useStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/views/model-management/useStore')>()
  return { ...actual, useStore: vi.fn() }
})
vi.mock('@/views/model-management/add-model/useUpload', () => ({
  useChunkedUpload: vi.fn(),
}))
vi.mock('@repo/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@repo/hooks')>()
  return {
    ...actual,
    useConfirm: vi.fn(),
    useDialogClose: vi.fn(() => vi.fn()),
    message: { error: vi.fn(), warning: vi.fn(), success: vi.fn() },
  }
})

import { validateManifest } from '@/api/model'
import { useStore } from '@/views/model-management/useStore'
import { useChunkedUpload } from '@/views/model-management/add-model/useUpload'
import { message, useConfirm, useDialogClose } from '@repo/hooks'
import type { ModelFiles } from '@/views/model-management/useStore'
import {
  useWizard,
  type SourceStepExpose,
  type StepFormExpose,
  type InfoFormValues,
  type SourceFormValues,
} from '@/views/model-management/add-model/useWizard'

const create = vi.fn()
const inspect = vi.fn()
const ensureUploaded = vi.fn()
const closeMock = vi.fn()

function makeFiles(overrides: Partial<ModelFiles> = {}): ModelFiles {
  return {
    success: true,
    format: 'safetensors',
    architecture: 'qwen2',
    size_gb: 15,
    quantization: null,
    is_moe: false,
    suggested_type: 'llm',
    ...overrides,
  } as ModelFiles
}

function sourceExpose(overrides: Partial<SourceFormValues> = {}): SourceStepExpose {
  // model 必须 reactive:useWizard 的 watch 依赖属性级响应追踪(真实 Step 用 useForm 的 reactive model)
  const model = reactive<SourceFormValues>({
    source: 'huggingface',
    source_url: 'Qwen/Qwen2.5-7B-Instruct',
    revision: '',
    local_mode: 'path',
    ...overrides,
  })
  return {
    validate: vi.fn().mockResolvedValue(undefined),
    params: () => model,
    files: () => [],
    folderName: () => 'Qwen2.5-7B-Instruct',
  }
}

function infoExpose(values: Partial<InfoFormValues> = {}): StepFormExpose<InfoFormValues> {
  const model = reactive<InfoFormValues>({
    name: '',
    model_type: 'llm',
    format: null,
    architecture: '',
    size_gb: null,
    quantization: null,
    is_moe: false,
    ...values,
  })
  return {
    validate: vi.fn().mockResolvedValue(undefined),
    params: () => model,
  }
}

function mountWizard(onCreated?: (record: unknown) => void) {
  let wizard!: ReturnType<typeof useWizard>
  const wrapper = mount({
    setup: () => {
      wizard = useWizard(onCreated as Parameters<typeof useWizard>[0])
      return () => h('div')
    },
  })
  return { wizard, wrapper }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(useStore).mockReturnValue({ create, inspect } as unknown as ReturnType<typeof useStore>)
  vi.mocked(useChunkedUpload).mockReturnValue({
    state: { phase: 'idle', percentage: 0, filePercentages: {} },
    ensureUploaded,
    abort: vi.fn(),
  } as unknown as ReturnType<typeof useChunkedUpload>)
  vi.mocked(useDialogClose).mockReturnValue(closeMock)
})

describe('useWizard 探测与提交编排', () => {
  it('来源表单校验失败:不发起探测', async () => {
    const { wizard, wrapper } = mountWizard()
    const step = sourceExpose()
    step.validate = vi.fn().mockRejectedValue(new Error('invalid'))
    wizard.sourceStep.value = step

    await wizard.handleInspect()
    expect(inspect).not.toHaveBeenCalled()
    expect(wizard.current.value).toBe(1)
    wrapper.unmount()
  })

  it('探测成功:预填模型信息(名称按路径最后一段兜底),进入步骤2', async () => {
    const { wizard, wrapper } = mountWizard()
    wizard.sourceStep.value = sourceExpose()
    wizard.infoStep.value = infoExpose()
    inspect.mockResolvedValue({ files: makeFiles(), feasibility: null })

    await wizard.handleInspect()

    expect(inspect).toHaveBeenCalledWith({
      source: 'huggingface',
      source_url: 'Qwen/Qwen2.5-7B-Instruct',
      revision: null,
    })
    const info = wizard.infoStep.value!.params()
    expect(info).toMatchObject({
      name: 'Qwen2.5-7B-Instruct', // 路径最后一段兜底,型号中的版本号段保留
      model_type: 'llm',
      format: 'safetensors',
      architecture: 'qwen2',
      size_gb: 15,
    })
    expect(message.success).toHaveBeenCalledWith('探测成功,已回显识别结果')
    expect(wizard.current.value).toBe(2)
    wrapper.unmount()
  })

  it('名称兜底:本地路径尾部的纯字母扩展名会被去掉', async () => {
    const { wizard, wrapper } = mountWizard()
    wizard.sourceStep.value = sourceExpose({
      source: 'local',
      source_url: '/data/models/Qwen3.8-27B-FP8.safetensors/',
      local_mode: 'path',
    })
    wizard.infoStep.value = infoExpose()
    inspect.mockResolvedValue({ files: makeFiles(), feasibility: null })

    await wizard.handleInspect()

    // 去 .safetensors 扩展名,但保留 Qwen3.8 这类型号内的点
    expect(wizard.infoStep.value!.params().name).toBe('Qwen3.8-27B-FP8')
    wrapper.unmount()
  })

  it('探测软失败(files.success=false):停留当前步不预填', async () => {
    const { wizard, wrapper } = mountWizard()
    wizard.sourceStep.value = sourceExpose()
    wizard.infoStep.value = infoExpose()
    inspect.mockResolvedValue({ files: makeFiles({ success: false, message: 'repo 不存在' }), feasibility: null })

    await wizard.handleInspect()

    expect(wizard.current.value).toBe(1)
    expect(wizard.infoStep.value.params().name).toBe('')
    expect(message.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('local upload 模式未选文件:警告并中断', async () => {
    const { wizard, wrapper } = mountWizard()
    wizard.sourceStep.value = sourceExpose({ source: 'local', source_url: '', local_mode: 'upload' })
    wizard.infoStep.value = infoExpose()

    await wizard.handleInspect()

    expect(message.warning).toHaveBeenCalledWith('请先选择要上传的模型文件夹')
    expect(inspect).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('local upload 模式:预检 fatal 提示并中断;通过后 staged 路径改写来源地址', async () => {
    const { wizard, wrapper } = mountWizard()
    const step = sourceExpose({ source: 'local', source_url: '', local_mode: 'upload' })
    const file = new File([new Uint8Array(8)], 'model.safetensors')
    step.files = () => [file]
    wizard.sourceStep.value = step
    wizard.infoStep.value = infoExpose()

    // fatal:提示且不继续
    vi.mocked(validateManifest).mockResolvedValueOnce({
      warnings: [{ severity: 'fatal', message: '缺少 config.json', file: 'config.json' }],
    } as Awaited<ReturnType<typeof validateManifest>>)
    await wizard.handleInspect()
    expect(message.error).toHaveBeenCalled()
    expect(inspect).not.toHaveBeenCalled()

    // warn:二次确认后继续上传,staged 路径写回
    vi.mocked(validateManifest).mockResolvedValueOnce({
      warnings: [{ severity: 'warn', message: '文件较大', file: null }],
    } as Awaited<ReturnType<typeof validateManifest>>)
    vi.mocked(useConfirm).mockResolvedValueOnce(undefined)
    ensureUploaded.mockResolvedValueOnce('/staged/qwen-abc')
    inspect.mockResolvedValueOnce({ files: makeFiles(), feasibility: null })

    await wizard.handleInspect()
    expect(ensureUploaded).toHaveBeenCalledWith([file], 'Qwen2.5-7B-Instruct')
    expect(inspect).toHaveBeenCalledWith({
      source: 'local',
      source_url: '/staged/qwen-abc',
      revision: null,
    })
    wrapper.unmount()
  })

  it('提交:人工修正合并进 files 随创建回传,成功后 onCreated + close', async () => {
    const onCreated = vi.fn()
    const { wizard, wrapper } = mountWizard(onCreated)
    wizard.sourceStep.value = sourceExpose()
    wizard.infoStep.value = infoExpose()
    inspect.mockResolvedValue({ files: makeFiles(), feasibility: null })
    create.mockResolvedValue({ id: 7, name: 'Qwen2.5-7B' })

    await wizard.handleInspect()
    // 人工修正量化方案
    wizard.infoStep.value.params().quantization = 'awq'
    await wizard.submit()

    expect(create).toHaveBeenCalledWith(
      {
        name: 'Qwen2.5-7B-Instruct',
        source: 'huggingface',
        source_url: 'Qwen/Qwen2.5-7B-Instruct',
        revision: '',
        model_type: 'llm',
      },
      expect.objectContaining({ quantization: 'awq', format: 'safetensors' }),
    )
    expect(message.success).toHaveBeenCalledWith('模型 Qwen2.5-7B-Instruct 已创建')
    expect(onCreated).toHaveBeenCalledWith({ id: 7, name: 'Qwen2.5-7B' })
    expect(closeMock).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('重探测中禁止提交;模型类别变更触发覆盖重探测(带旧 base 参数)', async () => {
    const { wizard, wrapper } = mountWizard()
    wizard.sourceStep.value = sourceExpose()
    wizard.infoStep.value = infoExpose()
    inspect.mockResolvedValue({ files: makeFiles(), feasibility: null })

    await wizard.handleInspect()
    expect(inspect).toHaveBeenCalledTimes(1)

    // 变更类别触发 reinspect(带 base payload + 新类别)
    wizard.infoStep.value.params().model_type = 'embedding'
    await nextTick()
    await vi.waitFor(() => expect(inspect).toHaveBeenCalledTimes(2))
    expect(inspect.mock.calls[1][0]).toMatchObject({ model_type: 'embedding', source: 'huggingface' })

    // reinspecting 期间提交被忽略
    create.mockClear()
    wizard.reinspecting.value = true
    await wizard.submit()
    expect(create).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('来源信息变更后旧探测结果失效(需重新探测)', async () => {
    const { wizard, wrapper } = mountWizard()
    const step = sourceExpose()
    wizard.sourceStep.value = step
    wizard.infoStep.value = infoExpose()
    inspect.mockResolvedValue({ files: makeFiles(), feasibility: null })

    await wizard.handleInspect()
    expect(wizard.inspected.value?.success).toBe(true)

    // 变更来源地址 → inspected/feasibility 清空
    step.params().source_url = 'Qwen/Qwen3-32B'
    await nextTick()
    expect(wizard.inspected.value).toBeNull()
    wrapper.unmount()
  })
})
