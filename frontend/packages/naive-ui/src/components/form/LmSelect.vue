<script setup lang="ts">
import { computed, useAttrs } from 'vue'
import { NSelect } from 'naive-ui'

defineOptions({ name: 'LmSelect', inheritAttrs: false })

/** 透传属性集合(v-model/选项/事件监听器等经 v-bind 原样转交 n-select;收宽类型以便归一化空值) */
const attrs = useAttrs() as Record<string, unknown>

// 业务约定：''、undefined、null 均视为“未选择”，统一归一化为 naive-ui 认可的空值
const isEmptyValue = (val: unknown) => val === '' || val === undefined || val === null

const bindAttrs = computed(() => {
  // 显式标注:对象展开不保留 index signature,需收宽回透传记录类型
  const normalized: Record<string, unknown> = { consistentMenuWidth: false, ...attrs }
  if (isEmptyValue(normalized.value)) {
    normalized.value = normalized.multiple ? [] : null
  }
  return normalized
})
</script>

<template>
  <n-select v-bind="bindAttrs">
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
  </n-select>
</template>
