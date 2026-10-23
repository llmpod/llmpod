import { useConfirm } from '@repo/hooks'
import type { ModelDeploymentOut } from '@/api/deployment'
import StepBasic from './steps/StepBasic.vue'
import StepConfirm from './steps/StepConfirm.vue'
import StepGpuSelect from './steps/StepGpuSelect.vue'
import { useWizardStore } from './useStore'

/** 向导步骤标题(与 n-steps 的 current 同为 1 起) */
export const WIZARD_STEPS = ['基本信息', '选择算力', '确认部署'] as const

/** 步骤组件暴露的门控接口(各步骤 defineExpose 实现) */
interface StepExpose {
  /** 门控:返回 true 才允许推进(Step3 在 validate 内完成提交) */
  validate?: () => Promise<boolean>
  /** 响应式门控:false 时禁用「下一步」按钮(未暴露则视为可点) */
  canNext?: boolean
  /** 禁用提示:canNext 为 false 时悬浮「下一步」按钮展示的原因(未暴露则禁用无提示) */
  nextDisabledTip?: string
}

/** 向导配置:入口预选模型(模型管理行操作「新建部署」跳入) */
export interface WizardOptions {
  /** 打开即回填第一步表单的模型主键;null 走常规新建 */
  initialModelId?: number | null
}

/**
 * 退出回调:确认部署成功时携带刚创建的部署记录(列表层据此直接打开部署详情),
 * 取消/关闭向导时不带参。
 */
export function useController(
  onExit: (created?: ModelDeploymentOut) => void,
  options: WizardOptions = {},
) {
  const store = useWizardStore()

  const stepComponents = [StepBasic, StepGpuSelect, StepConfirm]
  /** 当前步骤组件实例;shallowRef 避免 UnwrapRef 摊平 expose 的 canNext */
  const stepRef = shallowRef<StepExpose | null>(null)
  /** 下一步处理中(防重复点击;Step1→2 期间兼作拓扑加载反馈) */
  const advancing = ref(false)

  // 模块级单例状态跨次打开会残留,须在子步骤挂载前(同步 setup 期)复位并应用入口预选:
  // StepBasic 的 useForm 以 data() 快照初始化,晚了预填值会被快照错过(同 quick-setup F-001)
  store.reset()
  if (options.initialModelId != null) store.form.modelId = options.initialModelId

  /** 入口预选了模型(模型管理跳入):StepBasic 的模型 watch 会按模型拉字典/预检,跳过进页面预取 */
  const hasInitialModel = options.initialModelId != null

  onMounted(() => {
    // 失败时 HTTP 层已统一提示,这里吞掉异常避免未处理拒绝;重开向导会重新加载
    store.loadClusters().catch(() => {})
    store.loadModels().catch(() => {})
    // 引擎能力字典:进页面预取一次并缓存,引擎下拉过滤/展示名均从它派生;
    // 入口预选了模型时由 StepBasic 的模型 watch 按该模型加载(supports_model 按模型判定),避免重复请求
    if (!hasInitialModel) store.loadEngines().catch(() => {})
  })

  const nextText = computed(() => (store.currentStep.value === 3 ? '确认部署' : '下一步'))

  /** 下一步禁用:当前步骤声明了 canNext 且为 false(validate 仍作为点击时的兜底) */
  const nextDisabled = computed(() => stepRef.value?.canNext === false)

  /** 下一步禁用提示:由当前步骤随 canNext 一起暴露(仅按钮禁用时悬浮展示) */
  const nextDisabledTip = computed(() => stepRef.value?.nextDisabledTip)

  /** 下一步:先调当前步骤的 validate 门控,通过才推进;Step3 提交成功后携带创建记录退出向导 */
  async function next() {
    if (advancing.value) return
    advancing.value = true
    const fromStep = store.currentStep.value
    try {
      const ok = (await stepRef.value?.validate?.()) ?? true
      // validate 期间步骤可能已被其它路径改变,仅当步骤未变才推进,防止弹跳
      if (!ok || store.currentStep.value !== fromStep) return
      if (fromStep === 3) {
        // Step3 的 validate 内已完成提交与提示;携带创建记录退出,列表层直接打开部署详情
        onExit(store.lastCreated.value ?? undefined)
        return
      }
      if (fromStep === 1) {
        // 预取目标集群拓扑 + 基础部署预检(内部均容错),Step2 自行处理加载/失败态
        await Promise.all([store.loadTopology(), store.loadInspect()])
        // 默认选中推荐配置(已手动选过则不覆盖)
        store.applyRecommendedSelection()
      }
      if (fromStep === 2) {
        // 全量预检(engine+topology+gpu_selector):接口失败停留本步;存在 issues 时二次确认,确认后强制推进
        const result = await store.runFinalInspect()
        if (!result) return
        if (result.issues.length) {
          try {
            await useConfirm({
              title: result.deployable ? '部署预检存在告警' : '部署预检不通过',
              content: () =>
                h(
                  'div',
                  { class: 'flex flex-col gap-[.4rem]' },
                  result.issues.map((issue) => h('div', issue)),
                ),
              confirmText: '强制部署',
            })
          } catch {
            // 取消/关闭:停留本步
            return
          }
          // 确认强制部署:提交时 payload 带 force,跳过后端启发式约束
          store.markForced()
        }
      }
      store.goToStep(fromStep + 1)
    } finally {
      advancing.value = false
    }
  }

  function prev() {
    store.goToStep(store.currentStep.value - 1)
  }

  /** 返回:二次确认后退出向导,已填写内容不保存(下次打开由 onMounted reset 复位) */
  async function cancel() {
    try {
      await useConfirm({
        title: '确认返回',
        content: '离开后本次填写的内容将不会保存,是否继续?',
        confirmText: '确认',
        cancelText: '取消',
      })
    } catch {
      return
    }
    onExit()
  }

  return {
    currentStep: store.currentStep,
    stepComponents,
    stepRef,
    advancing,
    nextText,
    nextDisabled,
    nextDisabledTip,
    next,
    prev,
    cancel,
  }
}
