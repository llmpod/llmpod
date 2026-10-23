<script setup lang="ts">
import { message, useConfirm } from '@repo/hooks'
import StepBasic from '@/views/deploy/components/deploy-list/create-wizard/steps/StepBasic.vue'
import StepConfirm from '@/views/deploy/components/deploy-list/create-wizard/steps/StepConfirm.vue'
import StepGpuSelect from '@/views/deploy/components/deploy-list/create-wizard/steps/StepGpuSelect.vue'
import { useWizardStore } from '@/views/deploy/components/deploy-list/create-wizard/useStore'
import { useStore } from '../useStore'

/**
 * 一键部署步:内嵌部署列表「新建部署」向导的三个步骤(基本信息/选择算力/确认部署),
 * 隐藏其步骤条与底部按钮,由外层向导的「上一步/下一步」驱动(模式同 StepModel 内嵌添加模型向导)。
 */
const quickStore = useStore()
const wizard = useWizardStore()

const stepComponents = [StepBasic, StepGpuSelect, StepConfirm]
// 模板里使用的成员必须解构为顶层 ref(store 是普通对象,嵌套 ref 在模板不会自动解包)
const { currentStep: innerStep } = wizard

// 复位 + 预填必须同步完成(不能放 onMounted):StepBasic setup 时即回读 wizard.form 初始化表单
wizard.reset()
// 预填向导前两步已确定的模型与集群,用户仍可在「基本信息」步内调整
if (quickStore.model.value) wizard.form.modelId = Number(quickStore.model.value.id)
if (quickStore.cluster.value) wizard.form.clusterId = quickStore.cluster.value.id

onMounted(() => {
  // 失败时 HTTP 层已统一提示,这里吞掉异常避免未处理拒绝;步骤内可重试
  wizard.loadClusters().catch(() => {})
  wizard.loadModels().catch(() => {})
  // 引擎能力字典:预取一次并缓存,引擎下拉过滤/展示名均从它派生
  wizard.loadEngines().catch(() => {})
})

/** 内嵌步骤组件暴露的门控接口(各步骤 defineExpose 实现) */
interface InnerStepExpose {
  /** 门控:返回 true 才允许推进 */
  validate?: () => Promise<boolean>
  /** 响应式门控:false 时禁用「下一步」按钮(经模板 ref 代理已解包为布尔值) */
  canNext?: boolean
  /** 禁用提示:canNext 为 false 时悬浮「下一步」按钮展示的原因 */
  nextDisabledTip?: string
}

/** 当前内嵌步骤组件实例;shallowRef 避免 UnwrapRef 摊平 expose 的 canNext */
const stepRef = shallowRef<InnerStepExpose | null>(null)

/** 步骤切换方向:前进右入/后退左入,驱动过渡名切换 */
const forward = ref(true)
watch(innerStep, (to, from) => {
  forward.value = to >= from
})

/** 外层「下一步」按钮文案:内嵌最后一步为「确认部署」 */
const nextText = computed(() => (innerStep.value === 3 ? '确认部署' : '下一步'))

/** 外层「下一步」禁用态:内嵌步骤声明了 canNext 且为 false(validate 仍作点击时兜底) */
const canNext = computed(() => stepRef.value?.canNext !== false)

/** 外层「下一步」禁用提示:透传内嵌步骤的禁用原因 */
const nextDisabledTip = computed(() => stepRef.value?.nextDisabledTip)

/**
 * 外层「下一步」门控:驱动内嵌向导逐步推进。
 * 内嵌 1→2:预取目标集群拓扑 + 基础预检,默认选中推荐配置;
 * 内嵌 2→3:全量预检(接口失败停留本步),存在 issues 时二次确认,确认后提交带 force;
 * 内嵌第 3 步:短路直调 store 提交创建并同步部署记录(完成页展示),返回 true 放行外层进入完成页。
 */
async function validate(): Promise<boolean> {
  const from = innerStep.value
  // 内嵌第 3 步:直调 store 提交创建并同步完成页。不走 stepRef.validate——
  // StepConfirm 的 validate 内部同样会 submitCreate,先调它再直调会连发两次创建接口
  if (from === 3) {
    try {
      const result = await wizard.submitCreate()
      if (!result) return false
      quickStore.applyDeploy(result)
    }
    catch {
      // 接口异常已由 HTTP 层统一提示,停留本步可重试
      return false
    }
    message.success(`部署 ${wizard.form.name} 已创建`)
    return true
  }
  const ok = (await stepRef.value?.validate?.()) ?? true
  // validate 期间内嵌步骤可能已被其它路径改变,仅当步骤未变才推进,防止弹跳
  if (!ok || innerStep.value !== from) return false
  if (from === 1) {
    await Promise.all([wizard.loadTopology(), wizard.loadInspect()])
    wizard.applyRecommendedSelection()
    wizard.goToStep(2)
    return false
  }
  if (from === 2) {
    const result = await wizard.runFinalInspect()
    if (!result) return false
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
      }
      catch {
        // 取消/关闭:停留本步
        return false
      }
      // 确认强制部署:提交时 payload 带 force,跳过后端启发式约束
      wizard.markForced()
    }
    wizard.goToStep(3)
    return false
  }
  return false
}

/** 外层「上一步」钩子:内嵌步骤 >1 时先退回内嵌上一步,返回 true 表示已消费不退出外层步骤 */
function prevAction(): boolean {
  if (innerStep.value > 1) {
    wizard.goToStep(innerStep.value - 1)
    return true
  }
  return false
}

defineExpose({ canNext, nextDisabledTip, nextText, validate, prevAction })
</script>

<template>
  <!-- 内嵌第 2 步(选择算力)需占满视口剩余高度:Vue Flow 按容器百分比取高,
       高度链断裂时画布解析为 0 高(节点图空白,控制台 warn "parent container needs a width and a height");
       与独立向导 create-wizard/index.vue 的 h-[calc(100vh-23rem)] 同模式,
       偏移按本页 chrome 实测(标题/steps/按钮栏在 quick-setup 外层 ≈ 30rem)。
       overflow-y-auto 兜底:规格行换行时溢出部分滚出而非压画布;overflow-x-hidden 裁掉过渡瞬态横向溢出 -->
  <div
    class="flex flex-col"
    :class="{
      'h-[calc(100vh-30rem)] overflow-y-auto overflow-x-hidden': innerStep === 2,
    }"
  >
    <!-- 内嵌步骤过渡:前进新步从右滑入、后退从左滑入,节奏与外层向导一致 -->
    <Transition
      :name="forward ? 'step-slide-left' : 'step-slide-right'"
      mode="out-in"
    >
      <component
        :is="stepComponents[innerStep - 1]"
        ref="stepRef"
        :key="innerStep"
      />
    </Transition>
  </div>
</template>

<style scoped>
/* 步骤切换过渡:前进右入左出、后退左入右出,与外层 quick-setup 的 step-slide 一致 */
.step-slide-left-enter-active,
.step-slide-left-leave-active,
.step-slide-right-enter-active,
.step-slide-right-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}
.step-slide-left-enter-from {
  opacity: 0;
  transform: translateX(1.6rem);
}
.step-slide-left-leave-to {
  opacity: 0;
  transform: translateX(-1.6rem);
}
.step-slide-right-enter-from {
  opacity: 0;
  transform: translateX(-1.6rem);
}
.step-slide-right-leave-to {
  opacity: 0;
  transform: translateX(1.6rem);
}
</style>
