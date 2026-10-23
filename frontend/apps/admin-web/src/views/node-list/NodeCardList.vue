<script setup lang="ts">
import type { ComputeNode } from './useStore'
import NodeCard from './NodeCard.vue'

defineProps<{
  nodes: ComputeNode[]
  loading: boolean
  hasActiveQuery: boolean
}>()

const emit = defineEmits<{
  gpu: [node: ComputeNode]
  detail: [node: ComputeNode]
  reset: []
  register: []
}>()
</script>

<template>
  <n-spin :show="loading">
    <div
      v-if="nodes.length"
      class="grid grid-cols-[repeat(auto-fill,minmax(32rem,1fr))] gap-[1.2rem] py-[.4rem]"
    >
      <NodeCard
        v-for="node in nodes"
        :key="node.id"
        :node="node"
        @gpu="emit('gpu', $event)"
        @detail="emit('detail', $event)"
      />
    </div>
    <template v-else>
      <LmTableEmpty
        v-if="!hasActiveQuery"
        description="还没有节点,注册一个开始使用"
        action-text="注册节点"
        @action="emit('register')"
      />
      <LmTableEmpty
        v-else
        description="未找到匹配的结果"
        action-text="清空筛选"
        @action="emit('reset')"
      />
    </template>
  </n-spin>
</template>
