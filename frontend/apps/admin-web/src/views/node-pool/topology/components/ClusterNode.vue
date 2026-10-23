<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { ServerOutline } from '@vicons/ionicons5'
import NodeCard from './NodeCard.vue'
import type { ClusterNodeData } from '../useStore'

const props = defineProps<NodeProps>()
const cluster = computed(() => props.data.record as ClusterNodeData)
const detail = computed(() => Boolean(props.data.detail))
</script>

<template>
  <NodeCard
    :icon="ServerOutline"
    :title="cluster.name"
    :subtitle="cluster.type === 'k8s' ? 'Kubernetes 集群' : 'Docker 集群'"
    :status-color="cluster.status === 'active' ? 'bg-success' : 'bg-secondary'"
  >
    <div v-if="detail" class="flex justify-between">
      <span class="text-secondary">节点</span><span>{{ cluster.nodeCount }}</span>
    </div>
    <div v-if="detail" class="flex justify-between">
      <span class="text-secondary">GPU</span><span>{{ cluster.gpuTotal }}</span>
    </div>
  </NodeCard>
</template>
