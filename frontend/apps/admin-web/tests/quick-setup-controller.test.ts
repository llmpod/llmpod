// @vitest-environment jsdom
import { createApp, h } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// ---------- mocks(工厂内创建,经被 mock 模块导出供测试断言/控制) ----------

vi.mock('vue-router', () => {
  const router = {
    back: vi.fn(),
    replace: vi.fn(),
    options: { history: { state: { back: null as string | null } } },
  }
  return { useRouter: () => router }
})

vi.mock('@repo/hooks', () => ({
  message: { error: vi.fn() },
  useConfirm: vi.fn(() => Promise.resolve()),
}))

vi.mock('@/api/quick-setup', () => ({ QUICK_SETUP_SKIP_KEY: 'llmpod:quick-setup:skipped' }))

vi.mock('@/store/auth', () => {
  const auth = { firstMenuName: vi.fn(() => 'dashboard') }
  return { useAuthStore: () => auth }
})

vi.mock('@/views/quick-setup/useStore', async () => {
  const { ref } = await import('vue')
  const currentStep = ref(1)
  const impl = {
    currentStep,
    reset: vi.fn(),
    init: vi.fn(() => Promise.resolve()),
    startPolling: vi.fn(),
    stopPolling: vi.fn(),
    skipModelUpload: vi.fn(),
    goToStep: vi.fn((step: number) => {
      currentStep.value = step
    }),
  }
  return { useStore: () => impl }
})

// 步骤组件仅作占位(门控逻辑经 stepRef 注入桩实现)
vi.mock('@/views/quick-setup/steps/StepNodes.vue', () => ({ default: {} }))
vi.mock('@/views/quick-setup/steps/StepModel.vue', () => ({ default: {} }))
vi.mock('@/views/quick-setup/steps/StepDeploy.vue', () => ({ default: {} }))
vi.mock('@/views/quick-setup/steps/StepFinish.vue', () => ({ default: {} }))

import { message, useConfirm } from '@repo/hooks'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/store/auth'
import { useStore } from '@/views/quick-setup/useStore'
import { useController } from '@/views/quick-setup/useController'

const router = useRouter()
const auth = useAuthStore() as { firstMenuName: ReturnType<typeof vi.fn> }
const store = useStore() as ReturnType<typeof useStore> & {
  goToStep: ReturnType<typeof vi.fn>
}

/** 在真实组件 setup 内创建控制器(触发 onMounted/onBeforeUnmount 生命周期) */
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

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  router.options.history.state.back = null
  auth.firstMenuName.mockReturnValue('dashboard')
  store.currentStep.value = 1
})

// ---------- 生命周期 ----------

describe('挂载与卸载', () => {
  it('挂载时复位单例状态并初始化 + 启动轮询,卸载时停止轮询', () => {
    const { controller, unmount } = mountController()
    expect(store.reset).toHaveBeenCalledTimes(1)
    expect(store.init).toHaveBeenCalledTimes(1)
    expect(store.startPolling).toHaveBeenCalledTimes(1)

    unmount()
    expect(store.stopPolling).toHaveBeenCalledTimes(1)
    expect(controller.stepRef.value).toBeNull()
  })

  it('init 失败时提示「集群初始化失败」且不抛未处理拒绝', async () => {
    vi.mocked(store.init).mockRejectedValueOnce(new Error('boom'))
    mountController()
    await Promise.resolve()
    expect(message.error).toHaveBeenCalledWith('集群初始化失败,请刷新重试')
  })
})

// ---------- goHome(a8d4c85 登录页路径判断) ----------

describe('goHome 返回上一页逻辑', () => {
  it('SPA 内有上一页(非登录页)时 router.back', () => {
    router.options.history.state.back = '/deploy'
    const { controller } = mountController()
    controller.goHome()
    expect(router.back).toHaveBeenCalledTimes(1)
    expect(router.replace).not.toHaveBeenCalled()
  })

  it('上一页为 /login(登录后整页重定向进入)时回第一个有权限菜单', () => {
    router.options.history.state.back = '/login'
    const { controller } = mountController()
    controller.goHome()
    expect(router.back).not.toHaveBeenCalled()
    expect(router.replace).toHaveBeenCalledWith({ name: 'dashboard' })
  })

  it('上一页为 /login?redirect=x 与 /login/ 同样视为登录页', () => {
    const { controller } = mountController()
    router.options.history.state.back = '/login?redirect=/admin'
    controller.goHome()
    expect(router.replace).toHaveBeenCalledWith({ name: 'dashboard' })

    router.options.history.state.back = '/login/'
    controller.goHome()
    expect(router.replace).toHaveBeenCalledTimes(2)
    expect(router.back).not.toHaveBeenCalled()
  })

  it('路径仅前缀相似(如 /loginx)不算登录页,正常 back', () => {
    router.options.history.state.back = '/loginx'
    const { controller } = mountController()
    controller.goHome()
    expect(router.back).toHaveBeenCalledTimes(1)
    expect(router.replace).not.toHaveBeenCalled()
  })

  it('无上一页(history.state.back 为 null)时回第一个有权限菜单', () => {
    router.options.history.state.back = null
    const { controller } = mountController()
    controller.goHome()
    expect(router.replace).toHaveBeenCalledWith({ name: 'dashboard' })
  })

  it('无可见菜单时退到根路径重定向', () => {
    router.options.history.state.back = null
    auth.firstMenuName.mockReturnValue('')
    const { controller } = mountController()
    controller.goHome()
    expect(router.replace).toHaveBeenCalledWith({ path: '/' })
  })
})

describe('skip 返回(二次确认)', () => {
  it('确认通过后写会话跳过标记并返回首页', async () => {
    router.options.history.state.back = '/login'
    const { controller } = mountController()
    await controller.skip()
    expect(useConfirm).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem('llmpod:quick-setup:skipped')).toBe('1')
    expect(router.replace).toHaveBeenCalledWith({ name: 'dashboard' })
  })

  it('取消确认时不写标记也不离开', async () => {
    vi.mocked(useConfirm).mockRejectedValueOnce(new Error('cancel'))
    const { controller } = mountController()
    await controller.skip()
    expect(sessionStorage.getItem('llmpod:quick-setup:skipped')).toBeNull()
    expect(router.back).not.toHaveBeenCalled()
    expect(router.replace).not.toHaveBeenCalled()
  })
})

describe('skipModel 跳过模型上传', () => {
  it('标记跳过并直接进入步骤3(部署)', () => {
    const { controller } = mountController()
    controller.skipModel()
    expect(store.skipModelUpload).toHaveBeenCalledTimes(1)
    expect(store.goToStep).toHaveBeenCalledWith(3)
    expect(controller.currentStep.value).toBe(3)
  })
})

// ---------- 步骤门控(ee61a7a) ----------

describe('next 下一步门控', () => {
  it('当前步骤 validate 返回 false 时不推进', async () => {
    const { controller } = mountController()
    controller.stepRef.value = { validate: vi.fn().mockResolvedValue(false) }
    await controller.next()
    expect(store.goToStep).not.toHaveBeenCalled()
    expect(controller.currentStep.value).toBe(1)
  })

  it('validate 通过后推进到下一步', async () => {
    const { controller } = mountController()
    controller.stepRef.value = { validate: vi.fn().mockResolvedValue(true) }
    await controller.next()
    expect(controller.currentStep.value).toBe(2)
  })

  it('步骤未暴露 validate 时视为通过', async () => {
    const { controller } = mountController()
    controller.stepRef.value = {}
    await controller.next()
    expect(controller.currentStep.value).toBe(2)
  })

  it('validate 等待期间步骤已被其它路径改变时不推进(防弹跳)', async () => {
    const { controller } = mountController()
    let resolveValidate!: (ok: boolean) => void
    controller.stepRef.value = {
      validate: () => new Promise<boolean>((resolve) => (resolveValidate = resolve)),
    }
    const pending = controller.next()
    // validate 挂起期间提交部署完成等路径已推进步骤
    store.goToStep(2)
    resolveValidate(true)
    await pending
    // 不从 2 再推到 3
    expect(controller.currentStep.value).toBe(2)
    expect(store.goToStep).toHaveBeenCalledTimes(1)
  })

  it('advancing 期间重复点击 next 直接忽略', async () => {
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
})

describe('prev 上一步', () => {
  it('步骤声明 prevAction 且消费点击时不退出外层步骤', () => {
    const { controller } = mountController()
    store.goToStep(2)
    controller.stepRef.value = { prevAction: vi.fn(() => true) }
    controller.prev()
    expect(controller.currentStep.value).toBe(2)
  })

  it('未消费时退回上一步', () => {
    const { controller } = mountController()
    store.goToStep(2)
    controller.stepRef.value = { prevAction: vi.fn(() => false) }
    controller.prev()
    expect(controller.currentStep.value).toBe(1)
  })

  it('未声明 prevAction 时直接退回', () => {
    const { controller } = mountController()
    store.goToStep(3)
    controller.prev()
    expect(controller.currentStep.value).toBe(2)
  })
})

describe('按钮文案与禁用联动', () => {
  it('默认文案按步骤切换:步骤1 下一步,步骤3 提交部署', () => {
    const { controller } = mountController()
    expect(controller.nextText.value).toBe('下一步')
    store.goToStep(3)
    expect(controller.nextText.value).toBe('提交部署')
  })

  it('步骤可覆盖按钮文案(如内嵌向导的 探测模型源)', () => {
    const { controller } = mountController()
    controller.stepRef.value = { nextText: '探测模型源' }
    expect(controller.nextText.value).toBe('探测模型源')
  })

  it('canNext=false 时禁用下一步并透出禁用提示', () => {
    const { controller } = mountController()
    expect(controller.nextDisabled.value).toBe(false)

    controller.stepRef.value = { canNext: false, nextDisabledTip: '请先选择模型文件' }
    expect(controller.nextDisabled.value).toBe(true)
    expect(controller.nextDisabledTip.value).toBe('请先选择模型文件')
  })

  it('未暴露 canNext 的步骤按钮可点', () => {
    const { controller } = mountController()
    controller.stepRef.value = {}
    expect(controller.nextDisabled.value).toBe(false)
    expect(controller.nextDisabledTip.value).toBeUndefined()
  })
})
