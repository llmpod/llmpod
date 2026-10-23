<script setup lang="ts">
import { NInput, type InputProps } from 'naive-ui'

defineOptions({ name: 'LmTextarea', inheritAttrs: false })

withDefaults(defineProps<{ type?: InputProps['type'] }>(), { type: 'textarea' })
</script>

<template>
  <n-input
    :type="type"
    v-bind="$attrs"
  >
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
