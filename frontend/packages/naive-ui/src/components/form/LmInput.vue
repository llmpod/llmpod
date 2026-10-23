<script setup lang="ts">
import { NInput } from 'naive-ui'

defineOptions({ name: 'LmInput', inheritAttrs: false })
</script>

<template>
  <n-input v-bind="$attrs">
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
  </n-input>
</template>
