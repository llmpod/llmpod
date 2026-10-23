<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { ServerOutline } from '@vicons/ionicons5'
import NodeCard from '@/views/node-pool/topology/components/NodeCard.vue'
import type { TopologyCluster } from '@/api/topology'

/** 选择图集群卡 data.record:集群信息 + 统计(buildSelectGraph 注入) */
type ClusterRecord = TopologyCluster & { nodeCount: number; gpuTotal: number }

const props = defineProps<NodeProps>()
const cluster = computed(() => props.data.record as ClusterRecord)
</script>

<template>
  <NodeCard
    :icon="ServerOutline"
    :title="cluster.name"
    :subtitle="cluster.type === 'k8s' ? 'Kubernetes 集群' : 'Docker 集群'"
    :status-color="cluster.status === 'active' ? 'bg-success' : 'bg-secondary'"
  >
    <div class="flex justify-between">
      <span class="text-secondary">节点</span><span>{{ cluster.nodeCount }}</span>
    </div>
    <div class="flex justify-between">
      <span class="text-secondary">GPU</span><span>{{ cluster.gpuTotal }}</span>
    </div>
  </NodeCard>
</template>
