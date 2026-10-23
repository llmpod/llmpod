<script setup lang="ts">
import { useFlowGuide } from './useFlowGuide'

const { steps, runningKey, startGuide } = useFlowGuide()
</script>

<template>
  <LmAlert type="primary" :show-icon="false">
    <div class="flex items-center gap-y-[.2rem]">
      <span class="mr-[.4rem] text-default">部署大模型流程:</span>
      <template v-for="(step, index) in steps" :key="step.key">
        <span v-if="index > 0" class="mx-[.4rem] leading-[1.6] text-default">—</span>
        <n-button
          text
          class="!leading-[1.6]"
          type="primary"
          :disabled="!!runningKey && runningKey !== step.key"
          @click="startGuide(step)"
        >
          {{ step.label }}
        </n-button>
      </template>
    </div>
  </LmAlert>
</template>
