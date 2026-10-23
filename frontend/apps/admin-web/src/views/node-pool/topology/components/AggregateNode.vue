<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { LayersOutline } from '@vicons/ionicons5'
import NodeCard from './NodeCard.vue'
import type { AggregateNodeData } from '../useStore'

const props = defineProps<NodeProps>()
const aggregate = computed(() => props.data.record as AggregateNodeData)
</script>

<template>
  <NodeCard
    :icon="LayersOutline"
    :title="`节点 × ${aggregate.nodeCount}`"
    :subtitle="`GPU × ${aggregate.gpuTotal} · 在线 ${aggregate.onlineNodes}`"
  >
    <div class="text-primary">点击展开详细视图</div>
  </NodeCard>
</template>
