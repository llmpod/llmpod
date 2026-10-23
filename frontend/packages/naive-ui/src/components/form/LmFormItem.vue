<script setup lang="ts">
import { NFormItem } from 'naive-ui'

defineOptions({ name: 'LmFormItem', inheritAttrs: false })
</script>

<template>
  <n-form-item v-bind="$attrs">
    <template
      v-for="(_, name) in $slots"
      #[name]="slotData"
      :key="name"
    >
      <slot
        :name="name"
        v-bind="slotData ?? {}"
      />
    </template>
  </n-form-item>
</template>
