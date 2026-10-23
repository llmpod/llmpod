// @vitest-environment jsdom
import { createApp, h } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// ---------- mocks ----------

vi.mock('@repo/hooks', () => ({ useConfirm: vi.fn() }))

vi.mock('@/views/deploy/components/deploy-list/create-wizard/steps/StepBasic.vue', () => ({
  default: {},
}))
vi.mock('@/views/deploy/components/deploy-list/create-wizard/steps/StepGpuSelect.vue', () => ({
  default: {},
}))
vi.mock('@/views/deploy/components/deploy-list/create-wizard/steps/StepConfirm.vue', () => ({
  default: {},
}))

vi.mock('@/views/deploy/components/deploy-list/create-wizard/useStore', async () => {
  const { reactive, ref } = await import('vue')
  const currentStep = ref(1)
  const lastCreated = ref(null)
  const form = reactive({
    name: '',
    modelId: null as number | null,
    engine: 'auto',
    imageTag: null as string | null,
    maxModelLen: 8192 as number | null,
    clusterId: null as number | null,
    replicas: 1,
    kvCacheDtype: '',
  })
  const impl = {
    currentStep,
    lastCreated,
    form,
    reset: vi.fn(),
    loadClusters: vi.fn(() => Promise.resolve()),
    loadModels: vi.fn(() => Promise.resolve()),
    loadEngines: vi.fn(() => Promise.resolve()),
    loadTopology: vi.fn(() => Promise.resolve()),
    loadInspect: vi.fn(() => Promise.resolve()),
    applyRecommendedSelection: vi.fn(),
    runFinalInspect: vi.fn(),
    markForced: vi.fn(),
    goToStep: vi.fn((step: number) => {
      currentStep.value = step
    }),
  }
  return { useWizardStore: () => impl }
})

import { useConfirm } from '@repo/hooks'
import { useWizardStore } from '@/views/deploy/components/deploy-list/create-wizard/useStore'
import type { InspectDeploymentResult, ModelDeploymentOut } from '@/api/deployment'
import { useController } from '@/views/deploy/components/deploy-list/create-wizard/useController'

const store = useWizardStore() as unknown as {
  currentStep: { value: number }
  lastCreated: { value: ModelDeploymentOut | null }
  form: { modelId: number | null }
  goToStep: ReturnType<typeof vi.fn>
  runFinalInspect: ReturnType<typeof vi.fn>
  markForced: ReturnType<typeof vi.fn>
  applyRecommendedSelection: ReturnType<typeof vi.fn>
  loadTopology: ReturnType<typeof vi.fn>
  loadInspect: ReturnType<typeof vi.fn>
  loadClusters: ReturnType<typeof vi.fn>
  loadModels: ReturnType<typeof vi.fn>
  loadEngines: ReturnType<typeof vi.fn>
  reset: ReturnType<typeof vi.fn>
}
const confirmMock = vi.mocked(useConfirm)
const onExit = vi.fn()

function mountController(options?: Parameters<typeof useController>[1]) {
  let controller!: ReturnType<typeof useController>
  const app = createApp({
    setup: () => {
      controller = useController(onExit, options)
      return () => h('div')
    },
  })
  app.mount(document.createElement('div'))
  return { controller, unmount: () => app.unmount() }
}

function makeInspect(overrides: Partial<InspectDeploymentResult> = {}): InspectDeploymentResult {
  return {
    deployable: true,
    issues: [],
    recommended_cluster_id: 1,
    cross_node: false,
    recommendation: {
      engine: 'vllm',
      image: 'reg/vllm:latest',
      topology: { tp: 1, pp: 1, replicas: 1, nnodes: 1, max_model_len: null },
      vram_per_gpu_gb: 40,
    },
    cluster_gpus: [],
    node_support: [],
    recommended_placement: null,
    resolved: null,
    ...overrides,
  } as InspectDeploymentResult
}

beforeEach(() => {
  vi.clearAllMocks()
  store.currentStep.value = 1
  store.lastCreated.value = null
  store.form.modelId = null
})

describe('挂载', () => {
  it('挂载时复位向导状态,并行预取集群/模型/引擎字典,失败静默', async () => {
    store.loadClusters.mockRejectedValueOnce(new Error('boom'))
    const { unmount } = mountController()
    expect(store.reset).toHaveBeenCalledTimes(1)
    expect(store.loadClusters).toHaveBeenCalledTimes(1)
    expect(store.loadModels).toHaveBeenCalledTimes(1)
    expect(store.loadEngines).toHaveBeenCalledTimes(1)
    // 等一轮微任务确认 rejected 已被吞掉
    await Promise.resolve()
    unmount()
  })

  it('复位发生在 setup 期(先于子步骤挂载快照),非 onMounted', () => {
    // useForm 以 data() 快照初始化,reset 必须在 StepBasic setup 前执行;
    // mountController 内 useController 在 setup 同步调用,此时 reset 已被调用即满足时序
    const { unmount } = mountController()
    expect(store.reset).toHaveBeenCalledTimes(1)
    unmount()
  })

  it('initialModelId:setup 期复位后回填模型,并跳过 onMounted 的引擎预取(由模型 watch 按模型加载)', async () => {
    const { controller, unmount } = mountController({ initialModelId: 3 })
    expect(store.reset).toHaveBeenCalledTimes(1)
    expect(store.form.modelId).toBe(3)
    expect(store.loadEngines).not.toHaveBeenCalled()
    expect(store.loadClusters).toHaveBeenCalledTimes(1)
    expect(store.loadModels).toHaveBeenCalledTimes(1)
    await Promise.resolve()
    unmount()
    void controller
  })

  it('无预选模型时 onMounted 仍预取引擎字典(常规新建入口)', async () => {
    const { unmount } = mountController()
    expect(store.loadEngines).toHaveBeenCalledTimes(1)
    await Promise.resolve()
    unmount()
  })
})

describe('Step1 → Step2', () => {
  it('validate 通过后预取拓扑+基础预检并应用推荐选卡,再推进', async () => {
    const { controller } = mountController()
    controller.stepRef.value = { validate: vi.fn().mockResolvedValue(true) }

    await controller.next()

    expect(store.loadTopology).toHaveBeenCalledTimes(1)
    expect(store.loadInspect).toHaveBeenCalledTimes(1)
    expect(store.applyRecommendedSelection).toHaveBeenCalledTimes(1)
    expect(controller.currentStep.value).toBe(2)
  })

  it('validate 失败时不触发预取也不推进', async () => {
    const { controller } = mountController()
    controller.stepRef.value = { validate: vi.fn().mockResolvedValue(false) }

    await controller.next()

    expect(store.loadTopology).not.toHaveBeenCalled()
    expect(controller.currentStep.value).toBe(1)
  })
})

describe('Step2 → Step3 全量预检门控', () => {
  function toStep2(controller: ReturnType<typeof useController>) {
    store.currentStep.value = 2
    controller.stepRef.value = { validate: vi.fn().mockResolvedValue(true) }
  }

  it('预检通过且无 issues 时直接推进', async () => {
    const { controller } = mountController()
    toStep2(controller)
    store.runFinalInspect.mockResolvedValue(makeInspect())

    await controller.next()

    expect(useConfirm).not.toHaveBeenCalled()
    expect(store.markForced).not.toHaveBeenCalled()
    expect(controller.currentStep.value).toBe(3)
  })

  it('全量预检失败(返回 null)时停留本步', async () => {
    const { controller } = mountController()
    toStep2(controller)
    store.runFinalInspect.mockResolvedValue(null)

    await controller.next()

    expect(controller.currentStep.value).toBe(2)
    expect(store.goToStep).not.toHaveBeenCalled()
  })

  it('预检存在 issues 时二次确认:确认后标记强制部署并推进', async () => {
    const { controller } = mountController()
    toStep2(controller)
    store.runFinalInspect.mockResolvedValue(makeInspect({ issues: ['同子网约束不满足'] }))
    confirmMock.mockResolvedValueOnce(undefined)

    await controller.next()

    expect(useConfirm).toHaveBeenCalledTimes(1)
    expect(store.markForced).toHaveBeenCalledTimes(1)
    expect(controller.currentStep.value).toBe(3)
  })

  it('预检存在 issues 时二次确认:取消则停留本步且不标记强制', async () => {
    const { controller } = mountController()
    toStep2(controller)
    store.runFinalInspect.mockResolvedValue(
      makeInspect({ deployable: false, issues: ['显存不足'] }),
    )
    confirmMock.mockRejectedValueOnce(new Error('cancel'))

    await controller.next()

    expect(store.markForced).not.toHaveBeenCalled()
    expect(controller.currentStep.value).toBe(2)
  })

  it('二次确认按 deployable 切换标题(告警 vs 不通过)', async () => {
    const { controller } = mountController()
    toStep2(controller)
    store.runFinalInspect.mockResolvedValue(
      makeInspect({ deployable: true, issues: ['纯告警'] }),
    )
    confirmMock.mockResolvedValue(undefined)

    await controller.next()

    const options = confirmMock.mock.calls[0][0] as { title: string; confirmText: string }
    expect(options.title).toBe('部署预检存在告警')
    expect(options.confirmText).toBe('强制部署')
  })
})

describe('Step3 提交退出', () => {
  it('validate 通过后调用 onExit 退出向导且不再推进步骤;无创建记录时不带参', async () => {
    const { controller } = mountController()
    store.currentStep.value = 3
    controller.stepRef.value = { validate: vi.fn().mockResolvedValue(true) }

    await controller.next()

    expect(onExit).toHaveBeenCalledTimes(1)
    expect(onExit).toHaveBeenCalledWith(undefined)
    expect(store.goToStep).not.toHaveBeenCalled()
    expect(controller.currentStep.value).toBe(3)
  })

  it('提交成功(lastCreated 有值)时 onExit 携带创建记录,供列表层直接打开部署详情', async () => {
    const { controller } = mountController()
    store.currentStep.value = 3
    controller.stepRef.value = { validate: vi.fn().mockResolvedValue(true) }
    const created = { deployment_id: 'd-9' } as ModelDeploymentOut
    store.lastCreated.value = created

    await controller.next()

    expect(onExit).toHaveBeenCalledTimes(1)
    expect(onExit).toHaveBeenCalledWith(created)
  })

  it('Step3 validate 失败(提交异常)时不退出', async () => {
    const { controller } = mountController()
    store.currentStep.value = 3
    controller.stepRef.value = { validate: vi.fn().mockResolvedValue(false) }

    await controller.next()

    expect(onExit).not.toHaveBeenCalled()
  })
})

describe('其余导航与按钮状态', () => {
  it('prev 退回上一步;cancel 二次确认通过后退出', async () => {
    confirmMock.mockResolvedValueOnce(undefined)
    const { controller } = mountController()
    store.currentStep.value = 2
    controller.prev()
    expect(controller.currentStep.value).toBe(1)

    await controller.cancel()
    expect(onExit).toHaveBeenCalledTimes(1)
  })

  it('cancel 取消确认时不退出', async () => {
    confirmMock.mockRejectedValueOnce(new Error('cancel'))
    const { controller } = mountController()
    await controller.cancel()
    expect(onExit).not.toHaveBeenCalled()
  })

  it('advancing 期间重复点击 next 忽略', async () => {
    const { controller } = mountController()
    let resolveValidate!: (ok: boolean) => void
    controller.stepRef.value = {
      validate: () => new Promise<boolean>((resolve) => (resolveValidate = resolve)),
    }
    const first = controller.next()
    const second = controller.next()
    resolveValidate(true)
    await Promise.all([first, second])
    expect(store.goToStep).toHaveBeenCalledTimes(1)
    expect(controller.currentStep.value).toBe(2)
  })

  it('按钮文案:步骤1/2 为下一步,步骤3 为确认部署', () => {
    const { controller } = mountController()
    expect(controller.nextText.value).toBe('下一步')
    store.currentStep.value = 3
    expect(controller.nextText.value).toBe('确认部署')
  })

  it('canNext=false 时禁用并透出提示(ee61a7a)', () => {
    const { controller } = mountController()
    controller.stepRef.value = { canNext: false, nextDisabledTip: '请先选择 GPU' }
    expect(controller.nextDisabled.value).toBe(true)
    expect(controller.nextDisabledTip.value).toBe('请先选择 GPU')
  })
})
