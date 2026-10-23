<script setup lang="ts">
import { NSwitch } from 'naive-ui'

defineOptions({ name: 'LmSwitch', inheritAttrs: false })
</script>

<template>
  <n-switch v-bind="$attrs">
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
  </n-switch>
</template>
