<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { DocumentTextOutline, RocketOutline } from '@vicons/ionicons5'
import NodeCard from '@/views/node-pool/topology/components/NodeCard.vue'
import { modelBrandIcon } from '@/views/node-pool/topology/components/brandIcons'
import type { DeploymentNodeData, LogEntry } from '../graph'

const props = defineProps<NodeProps>()
/** 日志入口:弹窗打开全部署编排时间线;stop 避免同时触发节点选中面板 */
const emit = defineEmits<{ viewLogs: [entry: LogEntry] }>()
const deploy = computed(() => props.data.record as DeploymentNodeData)
/** 部署按模型名匹配品牌图标,未命中回退通用火箭图标 */
const icon = computed(() => modelBrandIcon(deploy.value.modelName) ?? RocketOutline)
</script>

<template>
  <!-- 拓扑属性(tp/pp/副本/节点/上下文)藏于点击后的右上角详情面板;
       编排子态不在卡片展示,由上方独立的编排进度卡(phase 节点)承载 -->
  <NodeCard
    :icon="icon"
    :title="deploy.name"
    :subtitle="deploy.modelName"
    :status-color="deploy.statusDot"
  >
    <div class="flex justify-between items-center">
      <span class="text-secondary">状态</span>
      <span class="flex items-center gap-[.4rem] min-w-0">
        <span>{{ deploy.statusLabel }}</span>
      </span>
    </div>
    <div class="flex justify-between">
      <span class="text-secondary">引擎</span><span>{{ deploy.engine }}</span>
    </div>
    <div class="flex justify-end">
      <n-button
        text
        type="primary"
        size="tiny"
        @click.stop="emit('viewLogs', { type: 'timeline' })"
      >
        <template #icon>
          <n-icon :component="DocumentTextOutline" />
        </template>
        编排日志
      </n-button>
    </div>
  </NodeCard>
</template>
