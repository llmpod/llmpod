<script setup lang="ts">
import { NInputNumber, type InputNumberProps } from 'naive-ui'

withDefaults(defineProps<{ showButton?: InputNumberProps['showButton'] }>(), { showButton: false })

defineOptions({ name: 'LmInputNumber', inheritAttrs: false })
</script>

<template>
  <n-input-number v-bind="$attrs" :show-button="showButton">
    <template v-for="(_, name) in $slots" #[name]="slotData" :key="name">
      <slot :name="name" v-bind="slotData ?? {}" />
    </template>
  </n-input-number>
</template>
