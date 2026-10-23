<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { LayersOutline } from '@vicons/ionicons5'
import NodeCard from '@/views/node-pool/topology/components/NodeCard.vue'
import type { ReplicaNodeData } from '../graph'

const props = defineProps<NodeProps>()
const replica = computed(() => props.data.record as ReplicaNodeData)

/** 引擎副标题:份内引擎摘要(v1 每份单引擎,如 full · tp=2 · nvidia);推导份标注来源 */
const subtitle = computed(() => {
  if (replica.value.engines.length) {
    const engine = replica.value.engines[0]
    return [engine.role, engine.parallel, engine.gpuArch].filter(Boolean).join(' · ')
  }
  return replica.value.derived ? '按分片分布推导' : ''
})
</script>

<template>
  <!-- 供给中(扩容新份/未就绪):整卡脉动 + 虚线边框,仅部署运行中才脉动(终态部署静态展示);三态徽标走状态点,副本 ID/端点等藏于点击后的详情面板 -->
  <div :class="replica.pulse ? 'replica-provisioning' : undefined">
    <NodeCard
      :icon="LayersOutline"
      :title="`副本${replica.seq}`"
      :subtitle="subtitle"
      :status-color="replica.statusDot"
      :dashed="replica.status === 'provisioning'"
    >
      <div class="flex justify-between">
        <span class="text-secondary">状态</span><span>{{ replica.statusLabel }}</span>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary">分片</span><span>{{ replica.shardCount }}</span>
      </div>
      <div v-if="replica.endpointCount" class="flex justify-between">
        <span class="text-secondary">端点</span><span>{{ replica.endpointCount }} 条</span>
      </div>
    </NodeCard>
  </div>
</template>
