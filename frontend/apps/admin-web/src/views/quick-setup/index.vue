<script setup lang="ts">
import { useController, WIZARD_STEPS } from './useController'

const {
  currentStep,
  stepComponents,
  stepRef,
  advancing,
  nextText,
  nextDisabled,
  nextDisabledTip,
  next,
  prev,
  skip,
} = useController()

/** 步骤切换方向:前进右入/后退左入,驱动过渡名切换 */
const forward = ref(true)
watch(currentStep, (to, from) => {
  forward.value = to >= from
})
</script>

<template>
  <div>
    <LmCard>
      <div class="text-[1.6rem] font-semibold text-strong">一键部署</div>

      <n-steps class="mt-[1.6rem]" :current="currentStep" size="small">
        <n-step v-for="title in WIZARD_STEPS" :key="title" :title="title" />
      </n-steps>

      <div class="mt-[2rem]">
        <!-- 步骤过渡:前进新步从右滑入、后退从左滑入,节奏与 node-pool view-slide 一致 -->
        <Transition :name="forward ? 'step-slide-left' : 'step-slide-right'" mode="out-in">
          <component :is="stepComponents[currentStep - 1]" ref="stepRef" :key="currentStep" />
        </Transition>
      </div>

      <!-- 步骤导航整体居中;返回居中,与上一步/下一步两侧各留大间距;返回需二次确认。
           窄屏(<1024px)mx-auto 吸收空隙:上一步贴左、返回居中、下一步贴右,不再溢出视口。
           阈值取 lg 而非 md:768px 侧栏占位后内容区仅 ~500px,容不下 19.1rem 双侧固定间距 -->
      <div
        v-if="currentStep < 4"
        class="flex items-center justify-center mt-[2rem] pt-[1.6rem] border-t border-line"
      >
        <n-button :disabled="currentStep === 1 || advancing" @click="prev"> 上一步 </n-button>
        <n-button :disabled="advancing" class="mx-auto lg:mx-[19.1rem]" @click="skip"> 返回 </n-button>
        <!-- disabled 按钮不派发鼠标事件,用外层 span 作 tooltip 触发器;仅禁用且有原因文案时展示 -->
        <n-tooltip trigger="hover" :disabled="!nextDisabled || !nextDisabledTip">
          <template #trigger>
            <span>
              <n-button
                type="primary"
                :loading="advancing"
                :disabled="nextDisabled"
                @click="next"
              >
                {{ nextText }}
              </n-button>
            </span>
          </template>
          {{ nextDisabledTip }}
        </n-tooltip>
      </div>
    </LmCard>
  </div>
</template>

<style scoped>
/* 步骤切换过渡:前进右入左出、后退左入右出,与 node-pool 的 view-slide 节奏一致 */
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

:deep(.n-steps) {
  width: 80%;
  margin: 0 auto;
}

/* 窄屏(≤767px):步骤条占满并允许横向滚动,避免 4 步标题挤压换行 */
@media (max-width: 767px) {
  :deep(.n-steps) {
    width: 100%;
    overflow-x: auto;
  }
}

:deep(.n-steps .n-step:last-child) {
  flex: 0;
}
</style>
