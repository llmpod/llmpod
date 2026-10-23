// @vitest-environment happy-dom
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ModelDeploymentOut } from '@/api/deployment'

/** 内嵌步骤 expose 的 validate spy(StepBasic/StepConfirm 各一个;StepGpuSelect 不在本文件用例路径上) */
const mocks = vi.hoisted(() => ({
  basicValidate: vi.fn(),
  confirmValidate: vi.fn(),
}))

vi.mock('@repo/hooks', () => ({
  useConfirm: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

// StepConfirm 的 mock 还原真实行为:validate 内部直调 submitCreate 后返回 true。
// 用于捕捉外层「先调内嵌 validate 又直调 store」的双发路径
vi.mock('@/views/deploy/components/deploy-list/create-wizard/steps/StepBasic.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      setup(_, { expose }) {
        expose({ validate: () => mocks.basicValidate() })
      },
      render: () => h('div'),
    }),
  }
})
vi.mock('@/views/deploy/components/deploy-list/create-wizard/steps/StepGpuSelect.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ render: () => h('div') }) }
})
vi.mock('@/views/deploy/components/deploy-list/create-wizard/steps/StepConfirm.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      setup(_, { expose }) {
        expose({ validate: () => mocks.confirmValidate() })
      },
      render: () => h('div'),
    }),
  }
})

vi.mock('@/views/deploy/components/deploy-list/create-wizard/useStore', async () => {
  const { reactive, ref } = await import('vue')
  const currentStep = ref(3)
  const form = reactive({
    name: 'qwen2-5-7b',
    modelId: 7,
    clusterId: 1,
    engine: 'auto',
    imageTag: null,
    maxModelLen: 8192,
    replicas: 1,
    kvCacheDtype: '',
  })
  const impl = {
    currentStep,
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
    submitCreate: vi.fn(),
    goToStep: vi.fn((step: number) => {
      currentStep.value = step
    }),
  }
  return { useWizardStore: () => impl }
})

vi.mock('@/views/quick-setup/useStore', async () => {
  const { ref } = await import('vue')
  const impl = {
    currentStep: ref(3),
    cluster: ref({ id: 1, name: 'gpu-shared' }),
    model: ref({ id: 'm-1' }),
    applyDeploy: vi.fn(),
  }
  return { useStore: () => impl }
})

import { message } from '@repo/hooks'
import { useWizardStore } from '@/views/deploy/components/deploy-list/create-wizard/useStore'
import { useStore as useQuickStore } from '@/views/quick-setup/useStore'
import StepDeploy from '@/views/quick-setup/steps/StepDeploy.vue'

const wizard = useWizardStore() as unknown as {
  currentStep: { value: number }
  form: { name: string }
  goToStep: ReturnType<typeof vi.fn>
  runFinalInspect: ReturnType<typeof vi.fn>
  submitCreate: ReturnType<typeof vi.fn>
  loadTopology: ReturnType<typeof vi.fn>
  loadInspect: ReturnType<typeof vi.fn>
  applyRecommendedSelection: ReturnType<typeof vi.fn>
}
const quickStore = useQuickStore() as unknown as { applyDeploy: ReturnType<typeof vi.fn> }

const created = { deployment_id: 'd-1', name: 'qwen2-5-7b' } as ModelDeploymentOut

function mountStep() {
  return mount(StepDeploy)
}

function exposeValidate(wrapper: ReturnType<typeof mountStep>) {
  return (wrapper.vm as unknown as { validate: () => Promise<boolean> }).validate
}

beforeEach(() => {
  vi.clearAllMocks()
  wizard.currentStep.value = 3
})

describe('内嵌第 3 步提交(一键部署)', () => {
  it('确认部署:只发一次创建接口,同步部署记录并放行进入完成页', async () => {
    // 还原真实 StepConfirm.validate 行为:内部直调 submitCreate(若外层也直调,则双发)
    mocks.confirmValidate.mockImplementation(async () => {
      await wizard.submitCreate()
      return true
    })
    wizard.submitCreate.mockResolvedValue(created)

    const wrapper = mountStep()
    await expect(exposeValidate(wrapper)()).resolves.toBe(true)

    // 核心回归:创建接口只发一次(修复前:内嵌 validate 一次 + 外层直调一次 = 2 次)
    expect(wizard.submitCreate).toHaveBeenCalledTimes(1)
    // 第 3 步不走内嵌 StepConfirm 的 validate(其内部会提交,与外层直调互斥)
    expect(mocks.confirmValidate).not.toHaveBeenCalled()
    // 部署记录同步完成页 + 成功提示
    expect(quickStore.applyDeploy).toHaveBeenCalledWith(created)
    expect(message.success).toHaveBeenCalledWith('部署 qwen2-5-7b 已创建')
    wrapper.unmount()
  })

  it('提交异常:返回 false 停留本步,不提示成功', async () => {
    wizard.submitCreate.mockRejectedValue(new Error('409 conflict'))

    const wrapper = mountStep()
    await expect(exposeValidate(wrapper)()).resolves.toBe(false)

    expect(wizard.submitCreate).toHaveBeenCalledTimes(1)
    expect(quickStore.applyDeploy).not.toHaveBeenCalled()
    expect(message.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('submitCreate 返回 undefined(表单/拓扑不完整守卫):返回 false 不推进', async () => {
    wizard.submitCreate.mockResolvedValue(undefined)

    const wrapper = mountStep()
    await expect(exposeValidate(wrapper)()).resolves.toBe(false)

    expect(quickStore.applyDeploy).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('内嵌前两步门控(回归)', () => {
  it('内嵌 1→2:走内嵌步骤 validate,通过后预取并推进内嵌步骤', async () => {
    wizard.currentStep.value = 1
    mocks.basicValidate.mockResolvedValue(true)

    const wrapper = mountStep()
    await expect(exposeValidate(wrapper)()).resolves.toBe(false)

    expect(mocks.basicValidate).toHaveBeenCalledTimes(1)
    expect(wizard.loadTopology).toHaveBeenCalledTimes(1)
    expect(wizard.loadInspect).toHaveBeenCalledTimes(1)
    expect(wizard.applyRecommendedSelection).toHaveBeenCalledTimes(1)
    expect(wizard.goToStep).toHaveBeenCalledWith(2)
    wrapper.unmount()
  })

  it('内嵌步骤 validate 失败:不预取不推进', async () => {
    wizard.currentStep.value = 1
    mocks.basicValidate.mockResolvedValue(false)

    const wrapper = mountStep()
    await expect(exposeValidate(wrapper)()).resolves.toBe(false)

    expect(wizard.loadTopology).not.toHaveBeenCalled()
    expect(wizard.goToStep).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
