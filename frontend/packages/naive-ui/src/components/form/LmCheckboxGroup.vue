<script setup lang="ts">
import { NCheckboxGroup, NCheckbox } from 'naive-ui'

defineOptions({ name: 'LmCheckboxGroup', inheritAttrs: false })

defineProps<{
  options?: Array<{
    label?: string
    value: string | number
    disabled?: boolean
  }>
}>()
</script>

<template>
  <n-checkbox-group v-bind="$attrs">
    <n-checkbox
      v-for="opt in options ?? []"
      :key="opt.value"
      :value="opt.value"
      :disabled="opt.disabled"
    >
      {{ opt.label }}
    </n-checkbox>
  </n-checkbox-group>
</template>
