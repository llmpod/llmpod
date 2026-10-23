<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { DesktopOutline } from '@vicons/ionicons5'
import NodeCard from './NodeCard.vue'
import { NODE_STATUS_MAP, type NodeNodeData } from '../useStore'

const props = defineProps<NodeProps>()
const nodeItem = computed(() => props.data.record as NodeNodeData)
const detail = computed(() => Boolean(props.data.detail))
const gpuSummary = computed(() => {
  const first = nodeItem.value.gpus[0]
  return first ? `${first.name} × ${nodeItem.value.gpus.length}` : '—'
})
const statusDot = computed(() => NODE_STATUS_MAP[nodeItem.value.status]?.dot ?? 'bg-secondary')
const statusLabel = computed(
  () => NODE_STATUS_MAP[nodeItem.value.status]?.label ?? nodeItem.value.status,
)
/** 离线节点运行时字段后端置 0,展示 — 以区分「离线」与「在线空闲(0%)」 */
const online = computed(() => nodeItem.value.status === 'online')
const cpuPercent = computed(() => Math.round(nodeItem.value.cpuPercent))
const memoryPercent = computed(() => Math.round(nodeItem.value.memoryPercent))
const memoryTip = computed(() => `已用 ${nodeItem.value.memoryUsedGb}/${nodeItem.value.memoryGb}GB`)
</script>

<template>
  <NodeCard
    :icon="DesktopOutline"
    :title="nodeItem.hostname"
    :subtitle="nodeItem.ip"
    :status-color="statusDot"
  >
    <template v-if="detail">
      <div class="flex justify-between">
        <span class="text-secondary">状态</span><span>{{ statusLabel }}</span>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary shrink-0">GPU</span>
        <n-tooltip trigger="hover">
          <template #trigger>
            <span class="truncate max-w-[50%]">{{ gpuSummary }}</span>
          </template>
          {{ gpuSummary }}
        </n-tooltip>
      </div>
      <div>
        <div class="flex justify-between">
          <span class="text-secondary">CPU</span><span>{{ online ? `${cpuPercent}%` : '—' }}</span>
        </div>
        <div v-if="online" class="h-[.6rem] rounded-full bg-fill-4 mt-[.3rem] overflow-hidden">
          <div class="h-full rounded-full bg-primary" :style="{ width: `${cpuPercent}%` }" />
        </div>
      </div>
      <div>
        <div class="flex justify-between" :title="online ? memoryTip : undefined">
          <span class="text-secondary">内存</span><span>{{ online ? `${memoryPercent}%` : '—' }}</span>
        </div>
        <div v-if="online" class="h-[.6rem] rounded-full bg-fill-4 mt-[.3rem] overflow-hidden">
          <div class="h-full rounded-full bg-primary" :style="{ width: `${memoryPercent}%` }" />
        </div>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary">部署</span><span>{{ nodeItem.deployCount }} 个</span>
      </div>
    </template>
  </NodeCard>
</template>
