<script setup lang="ts">
import { ref } from 'vue'
import { NForm, type FormInst } from 'naive-ui'

defineOptions({ name: 'LmForm', inheritAttrs: false })

interface Props {
  labelPlacement?: 'left' | 'top'
  labelWidth?: number | string
  requireMarkPlacement?: 'left' | 'right'
  showRequireMark?: boolean
}

withDefaults(defineProps<Props>(), {
  labelPlacement: 'top',
  labelWidth: 80,
  requireMarkPlacement: 'right',
})

const formInst = ref<FormInst | null>(null)

// 暴露 FormInst 核心方法,对齐 useForm 的 formRef: Ref<FormInst | null> 契约
defineExpose({
  validate: (...args: Parameters<FormInst['validate']>) => formInst.value!.validate(...args),
  restoreValidation: () => formInst.value?.restoreValidation(),
  invalidateLabelWidth: () => formInst.value?.invalidateLabelWidth(),
})
</script>

<template>
  <n-form
    ref="formInst"
    v-bind="$attrs"
    :label-placement="labelPlacement"
    :label-width="labelWidth"
    :require-mark-placement="requireMarkPlacement"
    :show-require-mark="showRequireMark"
  >
    <slot />
  </n-form>
</template>
