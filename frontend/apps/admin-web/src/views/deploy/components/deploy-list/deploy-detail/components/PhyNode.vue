<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { DocumentTextOutline, ServerOutline } from '@vicons/ionicons5'
import NodeCard from '@/views/node-pool/topology/components/NodeCard.vue'
import type { LogEntry, PhyNodeData } from '../graph'

const props = defineProps<NodeProps>()
/** 日志入口:弹窗打开按本节点过滤的编排时间线;stop 避免同时触发节点选中面板 */
const emit = defineEmits<{ viewLogs: [entry: LogEntry] }>()
const phy = computed(() => props.data.record as PhyNodeData)
</script>

<template>
  <!-- 节点/GPU 属性(主机名/IP/显卡明细/步骤消息)藏于点击后的右上角详情面板;
       卡片标题优先 logs/nodes 摘要的主机名(可读名),无摘要回退 node_id -->
  <NodeCard
    :icon="ServerOutline"
    :title="phy.badge?.hostname ?? phy.nodeId"
    :subtitle="phy.badge?.label ?? '等待调度日志'"
    :status-color="phy.statusDot"
  >
    <div class="flex justify-end">
      <n-button
        text
        type="primary"
        size="tiny"
        @click.stop="emit('viewLogs', { type: 'timeline', nodeId: phy.nodeId })"
      >
        <template #icon>
          <n-icon :component="DocumentTextOutline" />
        </template>
        节点日志
      </n-button>
    </div>
  </NodeCard>
</template>
