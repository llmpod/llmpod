<script setup lang="ts">
import { NTreeSelect } from 'naive-ui'

defineOptions({ name: 'LmTreeSelect', inheritAttrs: false })
</script>

<template>
  <n-tree-select v-bind="$attrs">
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
  </n-tree-select>
</template>
