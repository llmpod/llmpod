<script setup lang="ts">
import type { NodeProps } from '@vue-flow/core'
import { RocketOutline } from '@vicons/ionicons5'
import NodeCard from './NodeCard.vue'
import { modelBrandIcon } from './brandIcons'
import { DEPLOY_STATUS_MAP, type DeployNodeData } from '../useStore'

const props = defineProps<NodeProps>()
const deploy = computed(() => props.data.record as DeployNodeData)
const detail = computed(() => Boolean(props.data.detail))
const statusDot = computed(() => DEPLOY_STATUS_MAP[deploy.value.status]?.dot ?? 'bg-secondary')
const statusLabel = computed(() => DEPLOY_STATUS_MAP[deploy.value.status]?.label ?? deploy.value.status)
/** 部署按模型名匹配品牌图标,未命中回退通用火箭图标 */
const icon = computed(() => modelBrandIcon(deploy.value.modelName) ?? RocketOutline)
</script>

<template>
  <NodeCard
    :icon="icon"
    :title="deploy.name"
    :subtitle="deploy.modelName"
    :status-color="statusDot"
  >
    <template v-if="detail">
      <div class="flex justify-between">
        <span class="text-secondary">状态</span><span>{{ statusLabel }}</span>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary">副本</span><span>{{ deploy.replicas }}</span>
      </div>
      <div class="flex justify-between">
        <span class="text-secondary">GPU</span><span>× {{ deploy.gpuUuids.length }}</span>
      </div>
    </template>
  </NodeCard>
</template>
