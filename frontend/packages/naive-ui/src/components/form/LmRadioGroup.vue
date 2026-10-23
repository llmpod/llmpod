<script setup lang="ts">
import { NRadioGroup, NRadio, NRadioButton } from 'naive-ui'

defineOptions({ name: 'LmRadioGroup', inheritAttrs: false })

defineProps<{
  options?: Array<{
    label?: string
    value: string | number | boolean
    disabled?: boolean
  }>
  button?: boolean
}>()
</script>

<template>
  <n-radio-group v-bind="$attrs">
    <component
      :is="button ? NRadioButton : NRadio"
      v-for="opt in options ?? []"
      :key="String(opt.value)"
      :value="opt.value"
      :disabled="opt.disabled"
    >
      {{ opt.label }}
    </component>
  </n-radio-group>
</template>
