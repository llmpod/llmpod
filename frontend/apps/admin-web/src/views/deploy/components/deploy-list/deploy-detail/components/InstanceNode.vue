<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { DocumentTextOutline, HardwareChipOutline } from '@vicons/ionicons5'
import NodeCard from '@/views/node-pool/topology/components/NodeCard.vue'
import type { InstanceNodeData, LogEntry } from '../graph'

const props = defineProps<NodeProps>()
/** 日志入口:弹窗打开该实例容器 stdout;stop 避免同时触发节点选中面板 */
const emit = defineEmits<{ viewLogs: [entry: LogEntry] }>()
const inst = computed(() => props.data.record as InstanceNodeData)

/** 副标题:Head/Worker;多副本时附所属份序号(副本N,与副本列/端点下拉口径一致) */
const subtitle = computed(() => {
  const base = inst.value.head ? 'Head' : 'Worker'
  return inst.value.replicaCount > 1 ? `${base} · 副本${inst.value.replicaSeq}` : base
})
</script>

<template>
  <!-- 端点/容器/起停时间等实例属性藏于点击后的右上角详情面板 -->
  <NodeCard
    :icon="HardwareChipOutline"
    :title="`#${inst.rank} ${inst.role}`"
    :subtitle="subtitle"
    :status-color="inst.statusDot"
  >
    <div class="flex justify-between">
      <span class="text-secondary">状态</span><span>{{ inst.statusLabel }}</span>
    </div>
    <div class="flex justify-between">
      <span class="text-secondary shrink-0">节点</span>
      <!-- 节点 UUID 可能很长:溢出省略 + 悬浮提示完整值(同 NodeCard 标题处理) -->
      <n-tooltip trigger="hover">
        <template #trigger>
          <span class="truncate ml-[.8rem]">{{ inst.nodeId ?? '待调度' }}</span>
        </template>
        {{ inst.nodeId ?? '待调度' }}
      </n-tooltip>
    </div>
    <!-- 最近错误:卡片省略 + 悬浮完整提示;完整内容见点击后的详情面板 -->
    <n-tooltip v-if="inst.lastError" trigger="hover">
      <template #trigger>
        <div class="text-error truncate">{{ inst.lastError }}</div>
      </template>
      {{ inst.lastError }}
    </n-tooltip>
    <!-- 容器日志:待调度实例无容器可追踪,不显示;启动命令入口在点击后的实例详情面板 -->
    <div v-if="inst.nodeId" class="flex justify-end">
      <n-button
        text
        type="primary"
        size="tiny"
        @click.stop="emit('viewLogs', { type: 'console', instanceId: inst.instanceId })"
      >
        <template #icon>
          <n-icon :component="DocumentTextOutline" />
        </template>
        容器日志
      </n-button>
    </div>
  </NodeCard>
</template>
