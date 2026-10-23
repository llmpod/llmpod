<script setup lang="ts">
import { NDatePicker } from 'naive-ui'

defineOptions({ name: 'LmDatePicker', inheritAttrs: false })
</script>

<template>
  <n-date-picker v-bind="$attrs">
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
  </n-date-picker>
</template>
