import { onBeforeUnmount, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { message, useConfirm } from '@repo/hooks'
import { QUICK_SETUP_SKIP_KEY } from '@/api/quick-setup'
import { useAuthStore } from '@/store/auth'
import StepDeploy from './steps/StepDeploy.vue'
import StepFinish from './steps/StepFinish.vue'
import StepModel from './steps/StepModel.vue'
import StepNodes from './steps/StepNodes.vue'
import { useStore } from './useStore'

/** 向导步骤标题(与 n-steps 的 current 同为 1 起;集群由 init 自动确保,新建集群在步骤1 内嵌进入) */
export const WIZARD_STEPS = ['添加节点', '上传模型', '一键部署', '完成发布'] as const

/** 步骤组件暴露的门控接口(各步骤 defineExpose 实现) */
interface StepExpose {
  validate?: () => Promise<boolean>
  /** 响应式门控:false 时禁用「下一步」按钮(未暴露则视为可点)。
   * 步骤内 expose 的是 computed,经模板 ref 代理(proxyRefs)已解包为布尔值 */
  canNext?: boolean
  /** 禁用提示:canNext 为 false 时悬浮「下一步」按钮展示的原因(未暴露则禁用无提示) */
  nextDisabledTip?: string
  /** 「下一步」按钮文案覆盖(如内嵌添加模型向导的 探测模型源/确认提交;未暴露用默认) */
  nextText?: string
  /** 「上一步」钩子:返回 true 表示步骤内已消费(如内嵌向导退回上一步),不退出外层步骤 */
  prevAction?: () => boolean
}

export function useController() {
  const router = useRouter()
  const auth = useAuthStore()
  const store = useStore()

  const stepComponents = [StepNodes, StepModel, StepDeploy, StepFinish]
  /** 当前步骤组件实例(模板 ref,动态组件上同样生效;shallowRef 避免 UnwrapRef 摊平 expose 的 canNext) */
  const stepRef = shallowRef<StepExpose | null>(null)
  /** 下一步处理中(防重复点击) */
  const advancing = ref(false)

  onMounted(() => {
    // 模块级单例状态跨路由/二次进入会残留,挂载前先复位避免步骤错乱(F-001)
    store.reset()
    // 默认集群自动创建失败(如接口异常)时提示,向导停留在步骤1 可重试刷新
    store.init().catch(() => message.error('集群初始化失败,请刷新重试'))
    store.startPolling()
  })
  onBeforeUnmount(() => store.stopPolling())

  const nextText = computed(
    () => stepRef.value?.nextText ?? (store.currentStep.value === 3 ? '提交部署' : '下一步'),
  )

  /** 下一步禁用:当前步骤声明了 canNext 且为 false(validate 仍作为点击时的兜底) */
  const nextDisabled = computed(() => stepRef.value?.canNext === false)

  /** 下一步禁用提示:由当前步骤随 canNext 一起暴露(仅按钮禁用时悬浮展示) */
  const nextDisabledTip = computed(() => stepRef.value?.nextDisabledTip)

  /** 下一步:先调当前步骤的 validate 门控,通过才推进 */
  async function next() {
    if (advancing.value) return
    advancing.value = true
    const fromStep = store.currentStep.value
    try {
      const ok = (await stepRef.value?.validate?.()) ?? true
      // validate 期间步骤可能已被其它路径改变(如提交部署等待 running 时),
      // 仅当步骤未变才推进,防止弹跳
      if (ok && store.currentStep.value === fromStep) store.goToStep(fromStep + 1)
    }
    finally {
      advancing.value = false
    }
  }

  function prev() {
    // 步骤声明了 prevAction 且消费了本次点击(如内嵌向导退回内部上一步),则不退出外层步骤
    if (stepRef.value?.prevAction?.()) return
    store.goToStep(store.currentStep.value - 1)
  }

  /** 是否登录页路径(portal 登录页与 admin 同域,路径 /login) */
  const isLoginPath = (path: string): boolean => /^\/login(?:[/?#]|$)/.test(path)

  /** 默认返回上一页;无上一页或上一页为登录页(登录后整页重定向进入)时,回第一个有权限的菜单(无菜单退到根路径重定向) */
  function goHome() {
    // history.state.back 仅记录 SPA 内上一页;整页重定向进入(如登录后落地)时为 null
    const back = router.options.history.state.back
    if (typeof back === 'string' && back && !isLoginPath(back)) {
      router.back()
      return
    }
    const first = auth.firstMenuName()
    void router.replace(first ? { name: first } : { path: '/' })
  }

  /** 返回:二次确认后写会话标记并离开向导,本次会话守卫不再重定向 */
  async function skip() {
    try {
      await useConfirm({
        title: '确认返回',
        content: '离开后本次向导的填写进度不会保存,是否继续?',
        confirmText: '确认',
        cancelText: '取消',
      })
    } catch {
      return
    }
    sessionStorage.setItem(QUICK_SETUP_SKIP_KEY, '1')
    goHome()
  }

  /** 步骤2:稍后在模型管理上传,直接进入步骤3(部署仅展示不可提交) */
  function skipModel() {
    store.skipModelUpload()
    store.goToStep(3)
  }

  // currentStep 摊平返回:index.vue 解构后模板顶层自动解包
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
    skip,
    skipModel,
    goHome,
  }
}
