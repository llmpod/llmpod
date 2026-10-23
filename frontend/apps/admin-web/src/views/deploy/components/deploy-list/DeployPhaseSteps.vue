<script setup lang="ts">
import { DEPLOY_PHASE_STEPS } from './useStore'

/** current:当前步骤序号(1 起,对齐 DEPLOY_CREATE_PHASES;0 表示未知子态,不高亮任何步骤) */
defineProps<{ current: number }>()
// class 等透传属性作用于 n-steps 本体(如父级 min-w 保底宽度),而非外层包裹 div
defineOptions({ inheritAttrs: false })
</script>

<template>
  <!-- w-max + min-w-full:步骤条被 min-w 撑宽时同步撑开滚动容器;内容窄时保持满宽居中 -->
  <div class="h-[3rem] w-max min-w-full flex flex-col justify-center items-center pl-[.5rem]">
    <n-steps v-bind="$attrs" :current="current" size="small">
      <n-step v-for="step in DEPLOY_PHASE_STEPS" :key="step.key" :title="step.label" />
    </n-steps>
  </div>
</template>
